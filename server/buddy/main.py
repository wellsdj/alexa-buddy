"""HTTP + WebSocket server. Run with:  python -m buddy"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from .audio import pcm_to_wav
from .config import DATA_DIR, SECRET_KEYS, WEB_DIR, Settings
from .hub import Hub, Session
from .memory import CATEGORIES
from .spotify import SpotifyError
from .tts import TTSUnavailable

log = logging.getLogger("buddy")
PORT = int(os.environ.get("BUDDY_PORT", "8000"))


def local_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class Advertiser:
    """Announces _buddy._tcp on the LAN so the ESP32 can find the server by itself."""

    def __init__(self):
        self.zc = None
        self.info = None

    def start(self, port: int) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            log.info("zeroconf not installed; the ESP32 will need the server's IP address")
            return
        ip = local_ip()
        try:
            self.info = ServiceInfo(
                "_buddy._tcp.local.", "Buddy Server._buddy._tcp.local.",
                addresses=[socket.inet_aton(ip)], port=port,
                properties={"path": "/ws"}, server=f"buddy-{socket.gethostname().split('.')[0]}.local.")
            self.zc = Zeroconf()
            self.zc.register_service(self.info, allow_name_change=True)
            log.info("advertising _buddy._tcp on %s:%d", ip, port)
        except Exception as e:  # noqa: BLE001
            log.warning("mDNS advertising failed: %s", e)

    def stop(self) -> None:
        if self.zc:
            try:
                self.zc.unregister_service(self.info)
                self.zc.close()
            except Exception:  # noqa: BLE001
                pass


def _page(title: str, message: str) -> str:
    import html
    return ("<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><body style='margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:#000;color:#fff;font:17px/1.5 -apple-system,system-ui,sans-serif'><div style='max-width:420px;"
            f"padding:24px'><h1 style='font-size:28px;margin:0 0 10px'>{html.escape(title)}</h1>"
            f"<p style='color:#bbb;margin:0'>{html.escape(message)}</p></div>")


def create_app(settings: Settings | None = None, http=None, claude_http=None, stt=None) -> FastAPI:
    settings = settings or Settings(DATA_DIR)
    hub = Hub(settings, http, claude_http, stt)
    adv = Advertiser()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await hub.start()
        if os.environ.get("BUDDY_NO_MDNS") != "1":
            await asyncio.to_thread(adv.start, PORT)
        ip = local_ip()
        log.info("Buddy is up. Web page: http://localhost:%d  (ESP32 server address: %s port %d)", PORT, ip, PORT)
        yield
        await hub.stop()
        await asyncio.to_thread(adv.stop)

    app = FastAPI(title="Buddy", lifespan=lifespan)
    app.state.hub = hub
    app.state.settings = settings

    def check_token(token: str | None) -> bool:
        need = settings["device_token"]
        return not need or token == need

    @app.middleware("http")
    async def auth(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            token = request.headers.get("x-buddy-token") or request.query_params.get("token")
            if not check_token(token):
                return JSONResponse({"error": "token required"}, status_code=401)
        return await call_next(request)

    # ---------- websocket ----------
    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        role = ws.query_params.get("role", "ui")
        name = ws.query_params.get("name", role)[:40]
        if role not in ("ui", "device") or not check_token(ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        await ws.accept()
        s = Session(hub, ws, role, name)
        hub.add(s)
        await hub.broadcast_ui({"type": "event", "kind": "sessions"})
        if role == "ui":
            await hub.greet(s)
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if msg.get("bytes") is not None:
                    s.feed(msg["bytes"])
                elif msg.get("text"):
                    try:
                        data = json.loads(msg["text"])
                    except json.JSONDecodeError:
                        continue
                    await s.on_message(data)
        except WebSocketDisconnect:
            pass
        except RuntimeError as e:
            log.info("socket closed: %s", e)
        finally:
            hub.remove(s)
            hub.device_left(s)
            hub.record("disconnect", session=s.id, role=s.role)
            await hub.broadcast_ui({"type": "event", "kind": "sessions"})

    # ---------- Spotify login ----------
    def spotify_redirect() -> str:
        return f"http://127.0.0.1:{PORT}/spotify/callback"  # add exactly this in your Spotify app

    @app.get("/spotify/login")
    async def spotify_login():
        try:
            return RedirectResponse(hub.spotify.login_url(spotify_redirect()))
        except SpotifyError as e:
            return HTMLResponse(_page("Spotify", str(e)), status_code=400)

    @app.get("/spotify/callback")
    async def spotify_callback(code: str = "", state: str = "", error: str = ""):
        if error or not code:
            return HTMLResponse(_page("Spotify", f"Spotify didn't connect ({error or 'no code'}). "
                                                 "Close this tab and press Connect Spotify again."))
        try:
            await hub.spotify.finish_login(code, state, spotify_redirect())
        except SpotifyError as e:
            return HTMLResponse(_page("Spotify", str(e)), status_code=400)
        await hub.broadcast_ui({"type": "event", "kind": "settings"})
        who = hub.spotify.state.get("account")
        return HTMLResponse(_page("Spotify connected", f"Connected{' as ' + who if who else ''}. "
                                                       "You can close this tab and ask Buddy to play something."))

    @app.post("/api/update")
    async def update_buddy():
        """The Update Buddy button, from the web page: runs the same installer, detached, because it
        stops this server and starts the new version."""
        import subprocess
        log_path = settings.data_dir / "update.log"
        cmd = ("curl -fsSL https://raw.githubusercontent.com/wellsdj/alexa-buddy/main/install.sh | "
               f"BUDDY_NO_BROWSER=1 bash > '{log_path}' 2>&1")
        subprocess.Popen(["/bin/bash", "-c", cmd], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        hub.record("update_started")
        return {"ok": True}

    @app.post("/api/spotify/disconnect")
    async def spotify_disconnect():
        hub.spotify.disconnect()
        return {"ok": True}

    # ---------- Study Buddy (its own sign-in: a profile and a PIN) ----------
    from .study import MAX_UPLOAD_BYTES, StudyError
    study = hub.study

    def studying(request: Request) -> dict:
        who = study.who(request.headers.get("x-study-token"))
        if not who:
            raise HTTPException(401, "Sign in again.")
        return who

    def study_call(fn, *a):
        try:
            return fn(*a)
        except StudyError as e:
            raise HTTPException(400, str(e))

    @app.get("/study")
    async def study_page():
        return FileResponse(WEB_DIR / "study.html", headers={"Cache-Control": "no-cache"})

    @app.get("/study/api/profiles")
    async def study_profiles():
        return {"profiles": study.profiles()}

    @app.post("/study/api/profiles")
    async def study_add_profile(body: dict):
        p = study_call(study.add_profile, body.get("name", ""), str(body.get("pin", "")))
        return {"profile": p, "token": study_call(study.login, p["id"], str(body.get("pin", "")))}

    @app.post("/study/api/login")
    async def study_login(body: dict):
        token = study_call(study.login, body.get("profile", ""), str(body.get("pin", "")))
        return {"token": token, "profile": study.who(token)}

    @app.post("/study/api/logout")
    async def study_logout(request: Request):
        study.logout(request.headers.get("x-study-token", ""))
        return {"ok": True}

    @app.get("/study/api/me")
    async def study_me(request: Request):
        return {"profile": studying(request)}

    @app.post("/study/api/me/delete")
    async def study_delete_me(request: Request, body: dict):
        who = studying(request)
        study_call(study.login, who["id"], str(body.get("pin", "")))  # PIN again, to be sure
        study.remove_profile(who["id"])
        return {"ok": True}

    @app.get("/study/api/lessons")
    async def study_lessons(request: Request):
        return {"lessons": study.lessons(studying(request)["id"])}

    @app.get("/study/api/lessons/{lid}")
    async def study_lesson(request: Request, lid: str):
        return study_call(study.lesson, studying(request)["id"], lid)

    @app.delete("/study/api/lessons/{lid}")
    async def study_delete_lesson(request: Request, lid: str):
        study_call(study.delete_lesson, studying(request)["id"], lid)
        return {"ok": True}

    @app.post("/study/api/lessons/{lid}/retry")
    async def study_retry(request: Request, lid: str):
        pid = studying(request)["id"]
        lesson = study_call(study.lesson, pid, lid)
        lesson.update(status="waiting", error="", progress=0)
        study.save_lesson(pid, lesson)
        hub.study_worker.add(pid, lid)
        return {"ok": True}

    @app.post("/study/api/lessons/text")
    async def study_add_text(request: Request, body: dict):
        pid = studying(request)["id"]
        text = (body.get("text") or "").strip()
        if len(text.split()) < 15:
            raise HTTPException(400, "Paste a bit more than that: at least a few sentences from the lesson.")
        lesson = study.new_lesson(pid, body.get("subject", ""), body.get("date", ""), body.get("title", ""), "text")
        lesson["transcript"] = text[:120_000]
        study.save_lesson(pid, lesson)
        hub.study_worker.add(pid, lesson["id"])
        return lesson

    @app.get("/study/api/recordings")
    async def study_recordings(request: Request):
        pid = studying(request)["id"]
        from .study import INBOX_FOLDER, find_inbox_folder
        folder = find_inbox_folder()
        return {"recordings": hub.study_worker.recordings.listed(pid),
                "mine": study.recorder_owner() == pid, "folder": str(folder) if folder else "",
                "folder_name": INBOX_FOLDER}

    @app.post("/study/api/recordings/mine")
    async def study_recordings_mine(request: Request):
        study.set_recorder_owner(studying(request)["id"])
        await asyncio.to_thread(hub.study_worker.scan_folder)
        return {"ok": True}

    @app.post("/study/api/recordings/{rid}/use")
    async def study_use_recording(request: Request, rid: str, body: dict):
        pid = studying(request)["id"]
        return study_call(hub.study_worker.lesson_from_recording, pid, rid, (body.get("title") or "").strip()[:80])

    @app.delete("/study/api/recordings/{rid}")
    async def study_delete_recording(request: Request, rid: str):
        study_call(hub.study_worker.recordings.delete, studying(request)["id"], rid)
        return {"ok": True}

    # ---------- Start revising ----------
    from .practice import HELP_PROMPT, SKILLS, match_skills
    from .study import _parse_json
    practice = hub.practice

    def lesson_skills(lesson: dict) -> list[str]:
        skills = [x for x in lesson.get("skills") or [] if x in SKILLS]
        if not skills and lesson.get("status") == "ready":
            text = " ".join([lesson.get("subject", "")] + [t["name"] for t in lesson.get("topics", [])]
                            + [p for n in lesson.get("notes", []) for p in n.get("points", [])])
            if any(w in text.lower() for w in ("math", "algebra", "geometr", "angle", "fraction", "equation", "statistic")):
                skills = match_skills(text)
        return skills

    @app.post("/study/api/revise/start")
    async def revise_start(request: Request, body: dict):
        pid = studying(request)["id"]
        if body.get("lesson") == "all":
            skills = [x["skill"] for x in practice.progress.summary(pid)][:8]
            if not skills:
                raise HTTPException(400, "Revise a lesson first; then you can revise everything together.")
            return practice.start(pid, "all", skills)
        lesson = study_call(study.lesson, pid, body.get("lesson", ""))
        skills = lesson_skills(lesson)
        if not skills:
            return {"cards": True}  # not maths: the lesson's own questions instead
        return practice.start(pid, lesson["id"], skills)

    @app.post("/study/api/revise/answer")
    async def revise_answer(request: Request, body: dict):
        pid = studying(request)["id"]
        try:
            practice.question(body.get("question", ""), pid)
            return practice.answer(body.get("round", ""), body.get("question", ""), body.get("values") or [])
        except (KeyError, ValueError):
            raise HTTPException(400, "That question has expired. Start revising again.")

    @app.post("/study/api/revise/next")
    async def revise_next(request: Request, body: dict):
        studying(request)
        try:
            return practice.next(body.get("round", ""))
        except KeyError:
            raise HTTPException(400, "That round has expired. Start revising again.")

    @app.post("/study/api/revise/help")
    async def revise_help(request: Request, body: dict):
        pid = studying(request)["id"]
        try:
            lv = practice.question(body.get("question", ""), pid)
        except KeyError:
            raise HTTPException(400, "That question has expired.")
        lv.helped = True
        if lv.help_steps is None:
            from .practice import show_answer
            labels = [f.get("label") or "" for f in lv.q.fields]
            answer = ", ".join(f"{l} {show_answer(a)}{f.get('suffix') and ' ' + f['suffix'] or ''}".strip()
                               for l, a, f in zip(labels, lv.q.answers, lv.q.fields))
            try:
                r = await hub.llm.chat([{"role": "user", "content": HELP_PROMPT.format(
                    question=lv.q.text, answer=answer, method=lv.q.method or SKILLS[lv.skill].name)}],
                    tier="fast", json_mode=True, max_tokens=900)
                steps = [str(x).strip() for x in _parse_json(r.content).get("steps", []) if str(x).strip()]
            except Exception as e:  # noqa: BLE001
                log.warning("help failed: %s", e)
                steps = []
            lv.help_steps = steps or [lv.q.method or "Have a look at your notes for this topic, then try again."]
        return {"steps": lv.help_steps}

    @app.get("/study/api/progress")
    async def study_progress(request: Request):
        pid = studying(request)["id"]
        return {"skills": practice.progress.summary(pid), "week": practice.progress.recent(pid)}

    @app.post("/study/api/device/recording")
    async def study_device_recording(request: Request):
        """The pocket Buddy sends a finished recording here when it's back on Wi-Fi (the body is
        the audio file; ?recorded_at= is when it started, in seconds since 1970)."""
        if not check_token(request.headers.get("x-buddy-token") or request.query_params.get("token")):
            raise HTTPException(401, "token required")
        pid = study.recorder_owner()
        if not pid:
            raise HTTPException(409, "Nobody is set to receive recordings yet (open Study and choose “Send recordings to me”).")
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkstemp(suffix=".wav", dir=study.dir)[1])
        size = 0
        try:
            with tmp.open("wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        raise HTTPException(413, "too big")
                    f.write(chunk)
            try:
                at = float(request.query_params.get("recorded_at", "") or time.time())
            except ValueError:
                at = time.time()
            rec = hub.study_worker.add_recording(pid, tmp, at, "pocket", move=True)
        finally:
            tmp.unlink(missing_ok=True)
        return {"ok": True, "id": rec["id"]}

    @app.post("/study/api/lessons/audio")
    async def study_add_audio(request: Request):
        """The recording is the request body itself; details come in the query string."""
        pid = studying(request)["id"]
        q = request.query_params
        lesson = study.new_lesson(pid, q.get("subject", ""), q.get("date", ""), q.get("title", ""), "audio")
        path = study.upload_path(pid, lesson["id"])
        size = 0
        with path.open("wb") as f:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    f.close()
                    path.unlink(missing_ok=True)
                    study.delete_lesson(pid, lesson["id"])
                    raise HTTPException(413, "That recording is too big (over 400 MB).")
                f.write(chunk)
        if size < 1000:
            path.unlink(missing_ok=True)
            study.delete_lesson(pid, lesson["id"])
            raise HTTPException(400, "That file is empty.")
        hub.study_worker.add(pid, lesson["id"])
        return lesson

    # ---------- pages ----------
    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.middleware("http")
    async def fresh_pages(request, call_next):
        # Browsers check for a newer copy each time, so an update shows without a hard refresh.
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    # ---------- status & settings ----------
    @app.get("/api/status")
    async def status():
        return hub.status()

    @app.get("/api/settings")
    async def get_settings():
        return {"settings": settings.all(), "keys": settings.secret_status()}

    @app.post("/api/settings")
    async def post_settings(body: dict):
        applied = settings.update(body)
        if {"latitude", "longitude", "timezone"} & applied.keys():
            asyncio.create_task(hub.weather.fetch(force=True))
        return {"applied": applied}

    @app.post("/api/secrets")
    async def post_secret(body: dict):
        name = body.get("name")
        if name not in SECRET_KEYS:
            raise HTTPException(400, "unknown key")
        value = (body.get("value") or "").strip()
        settings.set_secret(name, value)
        if name == "ANTHROPIC_API_KEY":
            code, message = (await hub.llm.check()) if value else (
                None, "Claude key removed. Buddy can't answer questions until you add one.")
        elif name == "GROQ_API_KEY":
            await hub.listener.discover(force=True)
            code = getattr(hub.listener, "discover_status", None)
            message = ("Groq key removed. Buddy uses simple phrase rules to tell when you're talking to it."
                       if not value else
                       "Groq key works. Buddy uses it to tell when you're talking to it." if code == 200 else
                       "Groq didn't accept that key. Copy it again (it starts with gsk_)." if code in (401, 403) else
                       "Key saved, but Groq couldn't be reached to check it. Is the internet on?")
        else:
            hub.tts.eleven.blocked_until = 0
            quota = await hub.tts.eleven.refresh_quota()
            code = getattr(hub.tts.eleven, "quota_status", None)
            if not value:
                message = "ElevenLabs key removed. Buddy will use the offline voice."
            elif code == 200:
                left = (quota["limit"] - quota["used"]) if quota and quota.get("limit") else None
                message = "ElevenLabs key works." + (f" {left:,} characters left this month." if left is not None else "")
            elif code in (401, 403):
                message = ("Key saved, but ElevenLabs wouldn't confirm it. If Buddy's voice sounds robotic, "
                           "make a new key with Text to Speech access turned on.")
            else:
                message = "Key saved, but ElevenLabs couldn't be reached to check it."
        return {"keys": settings.secret_status(), "check": {"ok": code == 200, "message": message}}

    @app.get("/api/log")
    async def get_log():
        return list(hub.log)

    @app.get("/api/models")
    async def models():
        await hub.listener.discover(force=True)
        return hub.llm_status()

    # ---------- alarms ----------
    @app.get("/api/alarms")
    async def list_alarms():
        return hub.alarms.to_api()

    @app.post("/api/alarms")
    async def add_alarm(body: dict):
        kind = body.get("kind", "alarm")
        try:
            if kind == "timer":
                a = hub.alarms.add_timer(int(body["seconds"]), body.get("label", ""))
            elif kind == "reminder" and body.get("seconds"):
                a = hub.alarms.add_reminder_in(int(body["seconds"]), body.get("label", ""))
            else:
                a = hub.alarms.add_alarm(body["time"], body.get("repeat") or None, body.get("label", ""),
                                         body.get("date") or None, kind=kind)
        except (KeyError, ValueError) as e:
            raise HTTPException(400, f"bad alarm: {e}")
        return {"alarm": a.id, "confirm": hub.alarms.confirm_text(a)}

    @app.patch("/api/alarms/{alarm_id}")
    async def patch_alarm(alarm_id: str, body: dict):
        a = hub.alarms.set_enabled(alarm_id, bool(body.get("enabled", True)))
        if not a:
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/alarms/{alarm_id}")
    async def delete_alarm(alarm_id: str):
        if not hub.alarms.cancel(alarm_id):
            raise HTTPException(404)
        return {"ok": True}

    # ---------- memory ----------
    @app.get("/api/memory")
    async def list_memory():
        from dataclasses import asdict
        return {"items": [asdict(m) for m in hub.memory.list()], "status": hub.memory_mgr.last_status,
                "categories": CATEGORIES}

    @app.post("/api/memory")
    async def add_memory(body: dict):
        text = (body.get("text") or "").strip()
        if not text:
            raise HTTPException(400, "empty")
        m, created = hub.memory.add(text, body.get("category", "other"), pinned=bool(body.get("pinned", True)),
                                    source="user")
        return {"id": m.id, "created": created}

    @app.patch("/api/memory/{mid}")
    async def edit_memory(mid: int, body: dict):
        m = hub.memory.update(mid, body.get("text"), body.get("category"), body.get("pinned"))
        if not m:
            raise HTTPException(404)
        return {"ok": True}

    @app.delete("/api/memory/{mid}")
    async def delete_memory(mid: int):
        if not hub.memory.delete([mid]):
            raise HTTPException(404)
        return {"ok": True}

    @app.post("/api/memory/compact")
    async def compact_memory():
        return await hub.memory_mgr.compact(force=True)

    @app.post("/api/memory/learn-now")
    async def learn_now():
        transcript, hub.conv.transcript = hub.conv.transcript, []
        return await hub.memory_mgr.learn_from(transcript)

    # ---------- weather ----------
    @app.get("/api/weather")
    async def weather(day: str = "today"):
        data = await hub.weather.fetch()
        if not data:
            raise HTTPException(503, hub.weather.last_error or "weather unavailable")
        out = hub.weather.summarize(data, day)
        out["daily"] = data.get("daily")
        out["hourly"] = {k: v[:48] for k, v in data.get("hourly", {}).items()}
        return out

    @app.get("/api/geocode")
    async def geocode(q: str):
        try:
            return await hub.weather.geocode(q)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(502, str(e))

    # ---------- voice ----------
    @app.post("/api/say")
    async def say(body: dict):
        text = (body.get("text") or "").strip()
        targets = [s for s in hub.sessions.values() if body.get("session") in (None, s.id)]
        for s in targets:
            asyncio.create_task(s.speak(text))
        return {"sent_to": [s.id for s in targets]}

    @app.post("/api/tts/preview")
    async def tts_preview(body: dict):
        text = (body.get("text") or "Hello! I'm Buddy. This is how I sound.")[:300]
        pcm = bytearray()
        try:
            async for chunk in hub.tts.stream(text):
                pcm += chunk
        except TTSUnavailable as e:
            raise HTTPException(503, str(e))
        return Response(pcm_to_wav(bytes(pcm)), media_type="audio/wav",
                        headers={"X-Engine": hub.tts.last_engine})

    return app
