"""Spotify: play, pause, skip and "what's this song?", on whichever Spotify app or speaker you use.

Buddy talks to Spotify directly. You make a free Spotify app once (developer.spotify.com),
paste its Client ID into Buddy's settings and press "Connect Spotify". Buddy uses Spotify's
PKCE login, so no client secret is needed, and it only keeps a refresh token (in data/spotify.json).
Playback control needs Spotify Premium. Music plays on a Spotify app or speaker (Spotify
doesn't allow streaming its audio to other gadgets), so Buddy tells that device what to play.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse

import httpx

from .config import Settings

log = logging.getLogger("buddy.spotify")

AUTH = "https://accounts.spotify.com/authorize"
TOKEN = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = ("user-read-playback-state user-modify-playback-state user-read-currently-playing user-read-private "
          "user-read-email user-top-read")


class SpotifyError(Exception):
    pass


class Spotify:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self.path = settings.data_dir / "spotify.json"
        self.state = self._load()
        self._verifier: dict[str, str] = {}  # login "state" -> PKCE verifier
        self.ducked_from: int | None = None  # music volume before Buddy turned it down to talk
        self._duck_lock = asyncio.Lock()  # down and back up never overlap (or the music stays quiet)
        self.last_device = ""
        self.last_error = ""

    # ---------- saved login ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        self.path.write_text(json.dumps(self.state, indent=2))
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    @property
    def client_id(self) -> str:
        return (self.settings["spotify_client_id"] or "").strip()

    @property
    def connected(self) -> bool:
        return bool(self.client_id and self.state.get("refresh_token"))

    def status(self) -> dict:
        return {"client_id": bool(self.client_id), "connected": self.connected,
                "account": self.state.get("account", ""), "email": self.state.get("email", ""),
                "plan": self.state.get("plan", ""), "last_error": self.last_error, "device": self.last_device}

    def disconnect(self) -> None:
        self.state = {}
        self._save()

    # ---------- login (PKCE) ----------
    def login_url(self, redirect_uri: str) -> str:
        if not self.client_id:
            raise SpotifyError("Add your Spotify Client ID in Settings first.")
        verifier = secrets.token_urlsafe(64)[:96]
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self._verifier = {state: verifier}
        return AUTH + "?" + urllib.parse.urlencode({
            "client_id": self.client_id, "response_type": "code", "redirect_uri": redirect_uri,
            "code_challenge_method": "S256", "code_challenge": challenge, "scope": SCOPES, "state": state})

    async def finish_login(self, code: str, state: str, redirect_uri: str) -> None:
        verifier = self._verifier.pop(state, None)
        if not verifier:
            raise SpotifyError("That login link has expired. Press Connect Spotify again.")
        r = await self.client.post(TOKEN, data={
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": self.client_id, "code_verifier": verifier})
        if r.status_code != 200:
            raise SpotifyError(f"Spotify said no ({r.status_code}): {r.text[:200]}")
        self._store_token(r.json())
        try:
            me = await self._api("GET", "/me") or {}
            self.state["account"] = me.get("display_name") or me.get("id", "")
            self.state["email"] = me.get("email", "")
            self.state["plan"] = me.get("product", "")  # what Spotify says: "premium", "free", ...
            self._save()
        except SpotifyError:
            pass

    def _store_token(self, tok: dict) -> None:
        self.state["access_token"] = tok["access_token"]
        self.state["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
        if tok.get("refresh_token"):
            self.state["refresh_token"] = tok["refresh_token"]
        self._save()

    async def _token(self) -> str:
        if not self.connected:
            raise SpotifyError("Spotify isn't connected yet. Connect it in Buddy's settings.")
        if self.state.get("access_token") and time.time() < self.state.get("expires_at", 0):
            return self.state["access_token"]
        r = await self.client.post(TOKEN, data={"grant_type": "refresh_token", "client_id": self.client_id,
                                                "refresh_token": self.state["refresh_token"]})
        if r.status_code != 200:
            if r.status_code in (400, 401):
                self.state.pop("refresh_token", None)
                self._save()
            raise SpotifyError("Spotify logged me out. Press Connect Spotify in the settings again.")
        self._store_token(r.json())
        return self.state["access_token"]

    async def _api(self, method: str, path: str, **kw):
        token = await self._token()
        try:
            r = await self.client.request(method, API + path, headers={"Authorization": f"Bearer {token}"}, **kw)
        except httpx.HTTPError as e:
            raise SpotifyError(f"couldn't reach Spotify: {e}") from e
        if r.status_code == 204 or not r.content:
            return None
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
                reason, msg = err.get("reason", ""), err.get("message", "")
            except ValueError:
                reason, msg = "", r.text[:200]
            self.last_error = f"{r.status_code} {reason or msg}"
            if reason == "NO_ACTIVE_DEVICE" or r.status_code == 404:
                raise SpotifyError("NO_DEVICE")
            if r.status_code == 403 and "PREMIUM" in (reason + msg).upper():
                raise SpotifyError("Spotify isn't letting me in just yet. It says the account needs Premium, and "
                                   "it can take a few hours to notice. Let's try again a bit later.")
            raise SpotifyError(f"Spotify error {r.status_code}: {msg or reason}")
        return r.json()

    # ---------- devices ----------
    async def devices(self) -> list[dict]:
        return ((await self._api("GET", "/me/player/devices")) or {}).get("devices", [])

    async def _pick_device(self) -> dict | None:
        """The preferred device (by name, from settings), else the active one, else a computer,
        else the first. Skips devices Spotify says can't be controlled."""
        devs = [d for d in await self.devices() if not d.get("is_restricted")]
        if not devs:
            return None
        want = (self.settings["spotify_device"] or "").strip().lower()
        own = not want and self.settings["music_on_buddy"]
        if own:
            want = (self.settings["assistant_name"] or "Buddy").strip().lower()  # Buddy's own speaker
        if want:
            for d in devs:
                if want in (d.get("name") or "").lower():
                    return d
            if own:
                raise SpotifyError(self._speaker_missing())
        for d in devs:
            if d.get("is_active"):
                return d
        for d in devs:
            if (d.get("type") or "").lower() == "computer":
                return d
        return devs[0]

    def _speaker_missing(self) -> str:
        """Buddy's own speaker isn't in Spotify's list: say why, rather than play on the TV."""
        st = self.music.status() if getattr(self, "music", None) else {}
        name = self.settings["assistant_name"] or "Buddy"
        if not st.get("installed"):
            return ("My speaker isn't set up yet: run Update Buddy on the Mac (it needs Homebrew from brew dot sh). "
                    "Or turn off Play music through Buddy's speaker in the settings.")
        if not st.get("running"):
            return "My speaker's switched off at the moment. Restarting Buddy on the Mac should bring it back."
        return (f"Spotify can't see my speaker yet. Just once: open Spotify, tap the speakers icon and pick {name}, "
                "then ask me again.")

    async def _ready_device(self) -> dict:
        """Make sure a device is ready to play: choose it and hand playback to it if it isn't active."""
        d = await self._pick_device()
        if not d:
            raise SpotifyError("I can't see Spotify open anywhere. Pop it open on your phone or Mac and ask me "
                               "again, and I'll get it going.")
        if not d.get("is_active"):
            try:
                await self._api("PUT", "/me/player", json={"device_ids": [d["id"]], "play": False})
            except SpotifyError as e:
                log.info("couldn't hand playback to %s: %s", d.get("name"), e)
        self.last_device = d.get("name", "")
        return d

    async def _with_device(self, method: str, path: str, **kw):
        """Run a player command on the chosen device (picking and waking one if needed)."""
        d = await self._ready_device()
        sep = "&" if "?" in path else "?"
        try:
            return await self._api(method, f"{path}{sep}device_id={d['id']}", **kw)
        except SpotifyError as e:
            if str(e) != "NO_DEVICE":
                raise
            raise SpotifyError(f"Spotify on {d.get('name', 'that device')} isn't answering. Give the app a tap "
                               "to wake it up, then ask me again.") from e

    async def _is_playing(self, tries: int = 4) -> bool:
        for _ in range(tries):
            await asyncio.sleep(0.8)
            try:
                st = await self._api("GET", "/me/player")
            except SpotifyError:
                continue
            if st and st.get("is_playing"):
                return True
        return False

    # ---------- what Buddy can do ----------
    async def play(self, query: str = "", kind: str = "") -> str:
        """Play something by name, or just resume. Checks it really started. Returns what to say."""
        query = (query or "").strip()
        if not query:
            await self._with_device("PUT", "/me/player/play")
            return "" if await self._is_playing() else self._not_started()
        if _norm(re.sub(r"^(me|us)\s+", "", query.lower())) in GENERIC:
            return await self._play_my_music()
        kinds = [kind] if kind in ("track", "artist", "album", "playlist") else ["track", "artist", "playlist"]
        search = re.sub(r"^(some|a bit of|a little|a few|me)\s+", "", query.lower()) or query
        found = await self._api("GET", "/search", params={"q": search, "type": ",".join(kinds), "limit": 5})
        pick = _best(found or {}, query, kinds)
        if not pick:
            return f"I couldn't find {query} on Spotify."
        item_kind, item = pick
        body = {"uris": [item["uri"]]} if item_kind == "track" else {"context_uri": item["uri"]}
        try:
            await self._with_device("PUT", "/me/player/play", json=body)
            started = await self._is_playing()
        except SpotifyError as e:
            if item_kind == "track" or "anywhere" in str(e) or "answering" in str(e):
                raise
            started = False
        if not started and item_kind != "track":
            # Some playlists (Spotify's own, for example) won't play for apps like Buddy:
            # play the top songs for the same search instead.
            tracks = await self._api("GET", "/search", params={"q": search, "type": "track", "limit": 20})
            uris = [t["uri"] for t in ((tracks or {}).get("tracks") or {}).get("items") or [] if t and t.get("uri")]
            if uris:
                await self._with_device("PUT", "/me/player/play", json={"uris": uris})
                started = await self._is_playing()
                item_kind, item = "mix", {"name": query}
        if not started:
            return self._not_started()
        where = f" on {self.last_device}" if self.last_device else ""
        if item_kind == "track":
            artists = ", ".join(a["name"] for a in item.get("artists", [])[:2])
            return f"Playing {item['name']} by {artists}{where}." if artists else f"Playing {item['name']}{where}."
        if item_kind == "mix":
            return f"Here's some {item['name']}{where}."
        return f"Playing {item['name']}{where}."

    async def _play_my_music(self) -> str:
        """"Play some music": your own most-played songs, shuffled; else carry on what was last on."""
        try:
            top = await self._api("GET", "/me/top/tracks", params={"limit": 40, "time_range": "medium_term"})
            uris = [t["uri"] for t in (top or {}).get("items", []) if t and t.get("uri")]
        except SpotifyError:
            uris = []  # older login without that permission: reconnect Spotify to get it
        if uris:
            import random
            random.shuffle(uris)
            await self._with_device("PUT", "/me/player/play", json={"uris": uris})
            if await self._is_playing():
                where = f" on {self.last_device}" if self.last_device else ""
                return f"Here's a mix of your favourites{where}."
            return self._not_started()
        await self._with_device("PUT", "/me/player/play")
        return "Carrying on where you left off." if await self._is_playing() else self._not_started()

    def _not_started(self) -> str:
        dev = self.last_device or "your Spotify app"
        return (f"I asked Spotify to play on {dev}, but it hasn't started. Give the Spotify app a tap so it "
                "wakes up, then ask me again.")

    async def pause(self) -> str:
        self.ducked_from = None  # an asked-for pause stays paused (don't resume after Buddy talks)
        await self._with_device("PUT", "/me/player/pause")
        return ""

    async def skip(self, back: bool = False) -> str:
        await self._with_device("POST", "/me/player/previous" if back else "/me/player/next")
        return ""

    async def set_volume(self, percent: int) -> str:
        percent = max(0, min(100, int(percent)))
        await self._with_device("PUT", f"/me/player/volume?volume_percent={percent}")
        return f"Music at {percent} percent."

    async def now_playing(self) -> str:
        cur = await self._api("GET", "/me/player/currently-playing")
        item = (cur or {}).get("item")
        if not item:
            return "Nothing's playing on Spotify right now."
        artists = ", ".join(a["name"] for a in item.get("artists", [])[:2])
        return f"This is {item['name']} by {artists}." if artists else f"This is {item['name']}."

    # ---------- turning the music down while Buddy talks ----------
    async def duck(self) -> None:
        if not self.connected or self.ducked_from is not None:
            return
        async with self._duck_lock:
            if self.ducked_from is not None:
                return
            try:
                st = await self._api("GET", "/me/player")
                dev = (st or {}).get("device") or {}
                vol = dev.get("volume_percent")
                own = (self.settings["assistant_name"] or "Buddy").strip().lower()
                if st and st.get("is_playing") and (dev.get("name") or "").strip().lower() == own:
                    # Music on Buddy's own speaker: pause it (Buddy can't talk over itself), resume after.
                    self.ducked_from = -1
                    await self._api("PUT", "/me/player/pause")
                elif st and st.get("is_playing") and vol is not None and vol > 20 and dev.get("supports_volume", True):
                    self.ducked_from = vol
                    await self._api("PUT", "/me/player/volume?volume_percent=20")
            except SpotifyError as e:
                log.info("couldn't turn the music down: %s", e)

    async def unduck(self) -> None:
        async with self._duck_lock:  # waits for a turn-down still in progress
            vol, self.ducked_from = self.ducked_from, None
            if vol is None:
                return
            try:
                if vol == -1:
                    await self._api("PUT", "/me/player/play")  # carry on where it paused
                else:
                    await self._api("PUT", f"/me/player/volume?volume_percent={vol}")
            except SpotifyError as e:
                log.info("couldn't turn the music back up: %s", e)


GENERIC = {"music", "somemusic", "something", "anything", "asong", "somesongs", "songs", "tunes", "sometunes",
           "somethinggood", "mymusic", "mysongs", "myfavourites", "myfavorites", "somethingnice", "atune"}


def _norm(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _best(found: dict, query: str, kinds: list[str]) -> tuple[str, dict] | None:
    """Pick what to play. A name that matches exactly wins (so "Fred again" finds the artist
    "Fred again.."); otherwise the top song, or the top playlist for vague asks like "some bangers"."""
    q = query.lower()
    vague = any(w in q for w in ("some ", "bangers", "music", "songs", "vibes", "mix", "playlist", "chill", "hits"))
    exact = [k for k in ("artist", "track", "album", "playlist") if k in kinds]
    fallback = [k for k in (["playlist", "track", "artist", "album"] if vague else ["track", "artist", "album", "playlist"])
                if k in kinds]
    for k in exact:
        for item in ((found.get(k + "s") or {}).get("items") or []):
            if item and item.get("uri") and _norm(item.get("name")) == _norm(query):
                return k, item
    for k in fallback:
        items = [i for i in ((found.get(k + "s") or {}).get("items") or []) if i and i.get("uri")]
        if items:
            return k, items[0]
    return None
