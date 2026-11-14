"""Study Buddy online (studdy-buddy-uk.vercel.app).

The same pages and question engine as on Buddy's Mac, but everything is kept online in Vercel Blob,
so it works anywhere, even with the Mac switched off. Buddy's Mac still does the listening: it turns
lesson recordings into words for free and sends them here (see /study/api/sync/...).

Storage: blobs are never changed in place. Each save is a new blob (its name carries the time) and
reading takes the newest, so a read never sees an old cached copy. Answers are stored as empty blobs
whose names hold the details, and levels are worked out by replaying them.

Sign-in and questions use signed tokens rather than stored sessions, because each request may be
served by a different copy of this function.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import sys
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the server folder, for buddy.*
from buddy.practice import (HELP_PROMPT, SKILLS, apply_answer, catalogue_text, check_field,  # noqa: E402
                            describe_progress, make_question, match_skills, pick_next, show_answer, streak, summarise)
from buddy.study_text import LESSON_PROMPT, StudyError, _clean_name, _hash_pin, _parse_json, tidy  # noqa: E402

log = logging.getLogger("study")
ROUND = 10
TOKEN_DAYS = 120


# ---------- storage ----------
class BlobStore:
    """Vercel Blob (private)."""

    async def put(self, path: str, body: bytes = b"") -> None:
        from vercel import blob
        await blob.put_blob(path, body or b"-", access="private", allow_overwrite=True,
                            content_type="application/json", cache_control_max_age=60)

    async def get(self, path: str) -> bytes | None:
        from vercel import blob
        try:
            return (await blob.get_blob(path, access="private")).body
        except blob.BlobNotFoundError:
            return None

    async def list(self, prefix: str) -> list[str]:
        from vercel import blob
        return [item.pathname async for item in blob.query_blobs(prefix=prefix)]

    async def delete(self, paths: list[str]) -> None:
        from vercel import blob
        for i in range(0, len(paths), 900):
            await blob.delete_blob(paths[i:i + 900])


class MemoryStore:
    """For tests and running locally without Vercel."""

    def __init__(self):
        self.d: dict[str, bytes] = {}

    async def put(self, path, body=b""):
        self.d[path] = body or b"-"

    async def get(self, path):
        return self.d.get(path)

    async def list(self, prefix):
        return sorted(k for k in self.d if k.startswith(prefix))

    async def delete(self, paths):
        for p in paths:
            self.d.pop(p, None)


class NoStore:
    async def _no(self, *a, **k):
        raise StudyError("The website's storage isn't connected yet: in Vercel, open Storage, create a Blob store and connect it to this project.")
    put = get = list = delete = _no


def now_ms() -> int:
    return int(time.time() * 1000)


class Data:
    def __init__(self, store):
        self.s = store

    # -- versioned JSON documents: <prefix>/<13-digit time>.json, newest wins --
    async def save_doc(self, prefix: str, doc: dict) -> None:
        old = await self.s.list(prefix + "/")
        newest = max((int(x.rsplit("/", 1)[-1].split(".")[0]) for x in old), default=0)
        path = f"{prefix}/{max(now_ms(), newest + 1):013d}.json"  # always later than what's there
        await self.s.put(path, json.dumps(doc).encode())
        old = [x for x in old if x != path]
        if old:
            try:
                await self.s.delete(old)
            except Exception:  # noqa: BLE001 - tidying up can wait
                pass

    async def load_doc(self, prefix: str) -> dict | None:
        versions = sorted(await self.s.list(prefix + "/"))
        if not versions:
            return None
        body = await self.s.get(versions[-1])
        return json.loads(body) if body else None

    async def docs_under(self, prefix: str) -> list[dict]:
        """The newest version of every document under prefix (e.g. all of someone's lessons)."""
        newest: dict[str, str] = {}
        for p in await self.s.list(prefix):
            key = p.rsplit("/", 1)[0]
            if p > newest.get(key, ""):
                newest[key] = p
        bodies = await asyncio.gather(*(self.s.get(p) for p in newest.values()))
        return [json.loads(b) for b in bodies if b]

    # -- people --
    async def profiles(self) -> list[dict]:
        bodies = await asyncio.gather(*(self.s.get(p) for p in await self.s.list("profiles/")))
        return sorted((json.loads(b) for b in bodies if b), key=lambda p: p.get("created", 0))

    async def profile(self, pid: str) -> dict | None:
        b = await self.s.get(f"profiles/{pid}.json")
        return json.loads(b) if b else None

    async def owner(self) -> str:
        marks = sorted(await self.s.list("owner/"))
        profiles = await self.profiles()
        ids = {p["id"] for p in profiles}
        for m in reversed(marks):
            pid = m.rsplit("-", 1)[-1]
            if pid in ids:
                return pid
        return profiles[0]["id"] if len(profiles) == 1 else ""

    # -- answers: empty blobs named u/<pid>/ans/<time>-<skill>-<level>-<right>-<helped>-<lesson>-<nonce> --
    async def answers(self, pid: str) -> list[dict]:
        out, seen = [], set()
        for p in sorted(await self.s.list(f"u/{pid}/ans/")):
            parts = p.rsplit("/", 1)[-1].split("-")
            if len(parts) != 7 or parts[6] in seen:
                continue
            seen.add(parts[6])
            out.append({"t": int(parts[0]) / 1000, "skill": parts[1], "level": int(parts[2]), "right": parts[3] == "1",
                        "helped": parts[4] == "1", "lesson": parts[5]})
        return out

    async def skills(self, pid: str) -> dict:
        skills: dict = {}
        for a in await self.answers(pid):
            apply_answer(skills, a["skill"], a["level"], a["right"], a["helped"], a["t"])
        return skills


# ---------- signed tokens ----------
def _secret() -> bytes:
    """Signs sign-ins and questions. STUDY_SECRET if set; otherwise made from the storage key (also secret),
    so there's nothing extra to set up."""
    s = (os.environ.get("STUDY_SECRET", "") or os.environ.get("BLOB_READ_WRITE_TOKEN", "")
         or os.environ.get("ANTHROPIC_API_KEY", ""))
    if not s:
        raise HTTPException(500, "The website's storage isn't connected yet (Vercel → Storage → Blob).")
    return hashlib.sha256(b"study-buddy:" + s.encode()).digest()


def sync_key() -> str:
    """What Buddy's Mac sends to prove it's Buddy. Handed over once, when you connect with a code."""
    return os.environ.get("STUDY_SYNC_KEY", "") or hmac.new(_secret(), b"sync", hashlib.sha256).hexdigest()


def sign(data: dict) -> str:
    raw = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    mac = hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{mac}"


def unsign(token: str) -> dict | None:
    try:
        raw, mac = (token or "").rsplit(".", 1)
    except ValueError:
        return None
    if not hmac.compare_digest(mac, hmac.new(_secret(), raw.encode(), hashlib.sha256).hexdigest()[:32]):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except ValueError:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


# ---------- Claude ----------
async def claude(prompt: str, max_tokens: int = 6000) -> str:
    import anthropic
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise StudyError("The Claude key isn't set on the website yet (Vercel → Settings → Environment Variables).")
    client = anthropic.AsyncAnthropic(api_key=key, max_retries=1)
    r = await client.messages.create(model=os.environ.get("STUDY_MODEL", "claude-haiku-5-5"), max_tokens=max_tokens,
                                     messages=[{"role": "user", "content": prompt + "\n\nReply with a single JSON object and nothing else."}])
    return "".join(b.text for b in r.content if b.type == "text")


def create_app(store=None) -> FastAPI:
    app = FastAPI(title="Study Buddy")
    blob_ready = bool(os.environ.get("BLOB_READ_WRITE_TOKEN") or os.environ.get("BLOB_STORE_ID"))
    if store is None and not blob_ready and os.environ.get("VERCEL"):
        store = NoStore()  # on Vercel but storage not connected: say so, rather than forget everything
    data = Data(store or (BlobStore() if blob_ready else MemoryStore()))
    app.state.data = data

    @app.exception_handler(StudyError)
    async def study_error(_, e: StudyError):
        return JSONResponse({"detail": str(e)}, status_code=400)

    async def me(request: Request) -> dict:
        t = unsign(request.headers.get("x-study-token", ""))
        if not t or t.get("k") != "me":
            raise HTTPException(401, "Sign in again.")
        p = await data.profile(t["pid"])
        if not p:
            raise HTTPException(401, "Sign in again.")
        return p

    def public(p: dict) -> dict:
        return {"id": p["id"], "name": p["name"], "colour": p.get("colour", 0)}

    def login_token(p: dict) -> str:
        return sign({"k": "me", "pid": p["id"], "exp": time.time() + TOKEN_DAYS * 86400})

    def check_sync(request: Request) -> None:
        if not hmac.compare_digest(request.headers.get("x-sync-key", ""), sync_key()):
            raise HTTPException(401, "Wrong or missing sync key.")

    @app.get("/study/api/health")
    async def health():
        """What's set up (names only, never values), to help when something isn't working."""
        names = ("BLOB_READ_WRITE_TOKEN", "BLOB_STORE_ID", "ANTHROPIC_API_KEY", "STUDY_SECRET", "VERCEL_OIDC_TOKEN")
        out = {n: bool(os.environ.get(n)) for n in names}
        out["blob_like"] = sorted(k for k in os.environ if "BLOB" in k)
        try:
            await data.s.list("profiles/")
            out["storage_works"] = True
        except Exception as e:  # noqa: BLE001
            out["storage_works"] = False
            out["storage_error"] = f"{type(e).__name__}: {str(e)[:200]}"
        return out

    # ---------- people ----------
    @app.get("/study/api/profiles")
    async def profiles():
        return {"profiles": [public(p) for p in await data.profiles()]}

    @app.post("/study/api/profiles")
    async def add_profile(body: dict):
        name, pin = _clean_name(body.get("name", "")), str(body.get("pin", ""))
        if not name:
            raise StudyError("Type a name.")
        if not re.fullmatch(r"\d{4}", pin):
            raise StudyError("The PIN needs to be 4 numbers.")
        existing = await data.profiles()
        if any(p["name"].lower() == name.lower() for p in existing):
            raise StudyError("There's already someone with that name.")
        if len(existing) >= 12:
            raise StudyError("That's the most people Study Buddy can hold.")
        salt = secrets.token_hex(8)
        p = {"id": secrets.token_hex(4), "name": name, "salt": salt, "pin": _hash_pin(pin, salt),
             "colour": len(existing) % 6, "created": time.time()}
        await data.s.put(f"profiles/{p['id']}.json", json.dumps(p).encode())
        return {"profile": public(p), "token": login_token(p)}

    async def check_pin(p: dict, pin: str) -> None:
        recent = [x for x in await data.s.list(f"fails/{p['id']}/") if int(x.rsplit("/", 1)[-1]) > now_ms() - 60_000]
        if len(recent) >= 5:
            raise StudyError("Too many wrong tries. Wait a minute, then try again.")
        if _hash_pin(pin or "", p["salt"]) != p["pin"]:
            await data.s.put(f"fails/{p['id']}/{now_ms()}")
            raise StudyError("Wrong PIN.")

    @app.post("/study/api/login")
    async def login(body: dict):
        p = await data.profile(str(body.get("profile", "")))
        if not p:
            raise StudyError("That person isn't here any more.")
        await check_pin(p, str(body.get("pin", "")))
        return {"token": login_token(p), "profile": public(p)}

    @app.post("/study/api/logout")
    async def logout():
        return {"ok": True}  # tokens aren't stored; the page forgets it

    @app.get("/study/api/me")
    async def whoami(request: Request):
        return {"profile": public(await me(request))}

    @app.post("/study/api/me/delete")
    async def delete_me(request: Request, body: dict):
        p = await me(request)
        await check_pin(p, str(body.get("pin", "")))
        await data.s.delete(await data.s.list(f"u/{p['id']}/") + [f"profiles/{p['id']}.json"])
        return {"ok": True}

    # ---------- lessons ----------
    def lesson_list_item(x: dict) -> dict:
        return {k: x.get(k) for k in ("id", "title", "auto_title", "subject", "about", "teacher", "date", "status",
                                      "progress", "error", "created", "duration_s")} | {
            "topics": [t["name"] for t in x.get("topics", [])], "questions": len(x.get("questions", []))}

    @app.get("/study/api/lessons")
    async def lessons(request: Request):
        p = await me(request)
        xs = await data.docs_under(f"u/{p['id']}/lessons/")
        return {"lessons": sorted((lesson_list_item(x) for x in xs), key=lambda x: (x["date"] or "", x["created"]), reverse=True)}

    async def load_lesson(pid: str, lid: str) -> dict:
        if not re.fullmatch(r"[0-9a-f]{8}", lid or ""):
            raise StudyError("No such lesson.")
        x = await data.load_doc(f"u/{pid}/lessons/{lid}")
        if not x:
            raise StudyError("No such lesson.")
        return x

    async def write_notes(pid: str, x: dict) -> dict:
        """Claude reads the lesson and writes the notes (done when the lesson is first opened)."""
        x.update(status="writing", progress=0, error="", started=time.time())
        await data.save_doc(f"u/{pid}/lessons/{x['id']}", x)
        try:
            prompt = LESSON_PROMPT.format(skills=catalogue_text(), what="a transcript of a recording", what_cap="Transcript",
                                          subject=x.get("subject") or "school", text=x["transcript"])
            tidy(_parse_json(await claude(prompt)), x)
            if not x["notes"] and not x["questions"]:
                raise StudyError("There wasn't enough lesson in there to make notes from.")
            x.update(status="ready", progress=100, error="")
        except StudyError as e:
            x.update(status="failed", error=str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("notes failed")
            x.update(status="failed", error=f"Something went wrong making the notes ({type(e).__name__}). Try again.")
        await data.save_doc(f"u/{pid}/lessons/{x['id']}", x)
        return x

    @app.get("/study/api/lessons/{lid}")
    async def lesson(request: Request, lid: str):
        p = await me(request)
        x = await load_lesson(p["id"], lid)
        if x["status"] == "waiting" or (x["status"] == "writing" and time.time() - x.get("started", 0) > 180):
            x = await write_notes(p["id"], x)
        return x

    @app.delete("/study/api/lessons/{lid}")
    async def delete_lesson(request: Request, lid: str):
        p = await me(request)
        await load_lesson(p["id"], lid)
        await data.s.delete(await data.s.list(f"u/{p['id']}/lessons/{lid}/"))
        return {"ok": True}

    @app.post("/study/api/lessons/{lid}/retry")
    async def retry(request: Request, lid: str):
        p = await me(request)
        x = await load_lesson(p["id"], lid)
        x.update(status="waiting", error="", progress=0)
        await data.save_doc(f"u/{p['id']}/lessons/{lid}", x)
        return {"ok": True}

    @app.post("/study/api/lessons/audio")
    async def no_upload():
        raise StudyError("Recordings come in through Buddy: put them in the “Buddy lessons” folder on the Mac.")

    # ---------- recordings (sent up by Buddy's Mac) ----------
    @app.get("/study/api/recordings")
    async def recordings(request: Request):
        p = await me(request)
        recs = [r for r in await data.docs_under(f"u/{p['id']}/recs/") if not r.get("used_by")]
        recs.sort(key=lambda r: r["recorded_at"], reverse=True)
        return {"recordings": [{k: r.get(k) for k in ("id", "recorded_at", "status", "name", "subject", "about", "duration_s", "source")}
                               for r in recs],
                "mine": await data.owner() == p["id"], "folder": "Buddy lessons", "folder_name": "Buddy lessons",
                "can_upload": False, "cloud": True}

    @app.post("/study/api/recordings/mine")
    async def recordings_mine(request: Request):
        p = await me(request)
        await data.s.put(f"owner/{now_ms():013d}-{p['id']}")
        return {"ok": True}

    @app.post("/study/api/recordings/{rid}/use")
    async def use_recording(request: Request, rid: str, body: dict):
        p = await me(request)
        if not re.fullmatch(r"[0-9a-f]{8}", rid or ""):
            raise StudyError("No such recording.")
        rec = await data.load_doc(f"u/{p['id']}/recs/{rid}")
        if not rec:
            raise StudyError("No such recording.")
        if rec.get("used_by"):
            raise StudyError("Notes have already been made from that recording.")
        lesson = {"id": secrets.token_hex(4), "subject": rec.get("subject", ""),
                  "date": time.strftime("%Y-%m-%d", time.gmtime(rec["recorded_at"] + int(body.get("tz_offset_s") or 0))),
                  "title": (body.get("title") or "").strip()[:80], "source": "audio", "created": time.time(), "status": "waiting",
                  "progress": 0, "error": "", "transcript": rec["transcript"], "summary": "", "notes": [], "topics": [],
                  "questions": [], "duration_s": rec.get("duration_s", 0), "about": rec.get("about", ""),
                  "teacher": rec.get("teacher", "")}
        if body.get("date"):
            lesson["date"] = str(body["date"])[:10]
        await data.save_doc(f"u/{p['id']}/lessons/{lesson['id']}", lesson)
        rec["used_by"] = lesson["id"]
        await data.save_doc(f"u/{p['id']}/recs/{rid}", rec)
        return lesson

    @app.delete("/study/api/recordings/{rid}")
    async def delete_recording(request: Request, rid: str):
        p = await me(request)
        if re.fullmatch(r"[0-9a-f]{8}", rid or ""):
            await data.s.delete(await data.s.list(f"u/{p['id']}/recs/{rid}/"))
        return {"ok": True}

    @app.post("/study/api/sync/recording")
    async def sync_recording(request: Request, body: dict):
        """Buddy's Mac sends a checked lesson recording (already turned into words)."""
        check_sync(request)
        pid = await data.owner()
        if not pid:
            raise HTTPException(409, "Nobody on Study Buddy is set to receive recordings yet.")
        rid = str(body.get("id") or secrets.token_hex(4))
        if not re.fullmatch(r"[0-9a-f]{8}", rid):
            rid = secrets.token_hex(4)
        if await data.load_doc(f"u/{pid}/recs/{rid}"):
            return {"ok": True, "id": rid}  # already have it
        rec = {"id": rid, "recorded_at": float(body.get("recorded_at") or time.time()), "status": "ready",
               "name": str(body.get("name", "Lesson"))[:60], "subject": _clean_name(str(body.get("subject", ""))),
               "about": str(body.get("about", ""))[:80], "duration_s": int(body.get("duration_s") or 0),
               "transcript": str(body.get("transcript", ""))[:120_000], "source": str(body.get("source", "pocket"))[:20],
               "teacher": _clean_name(str(body.get("teacher") or "")), "created": time.time(), "used_by": ""}
        await data.save_doc(f"u/{pid}/recs/{rid}", rec)
        return {"ok": True, "id": rid}

    @app.get("/study/api/sync/progress")
    async def sync_progress(request: Request, name: str = ""):
        """For Buddy to answer "how am I doing?"."""
        check_sync(request)
        profiles = await data.profiles()
        if not profiles:
            return {"text": "Nobody has a Study Buddy profile yet."}
        want = name.strip().lower()
        owner = await data.owner()
        p = (next((x for x in profiles if x["name"].lower() == want), None)
             or next((x for x in profiles if want and x["name"].lower().startswith(want.split()[0])), None)
             or next((x for x in profiles if x["id"] == owner), None) or profiles[0])
        lessons = [x for x in await data.docs_under(f"u/{p['id']}/lessons/") if x.get("status") == "ready"]
        lessons.sort(key=lambda x: x.get("date", ""), reverse=True)
        answers = await data.answers(p["id"])
        return {"text": describe_progress(p["name"], lessons, summarise(await data.skills(p["id"])), week(answers))}

    @app.post("/study/api/sync/pair")
    async def pair(body: dict):
        """Buddy's Mac swaps a pairing code (shown on this website) for the sync key."""
        code = re.sub(r"\W", "", str(body.get("code", ""))).upper()
        if not code:
            raise StudyError("Type the code shown on the Study Buddy website.")
        found = [x for x in await data.s.list(f"pair/{code}-") if now_ms() - int(x.rsplit("-", 1)[-1].split(".")[0]) < 15 * 60_000]
        if not found:
            raise StudyError("That code didn't work. Make a new one on the website and try again.")
        pid = found[0].split("/", 1)[1].split("-")[1]
        await data.s.delete(found)
        await data.s.put(f"owner/{now_ms():013d}-{pid}")
        return {"key": sync_key()}

    @app.post("/study/api/pair-code")
    async def pair_code(request: Request):
        p = await me(request)
        code = "".join(random.SystemRandom().choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        await data.s.put(f"pair/{code}-{p['id']}-{now_ms()}")
        return {"code": code, "minutes": 15}

    # ---------- revising ----------
    def week(answers: list[dict]) -> dict:
        cut = time.time() - 7 * 86400
        xs = [a for a in answers if a["t"] >= cut]
        return {"answered": len(xs), "right": sum(1 for a in xs if a["right"]),
                "days": len({time.strftime("%Y-%m-%d", time.gmtime(a["t"])) for a in xs}),
                "streak": streak([a["t"] for a in answers])}

    def lesson_skills(x: dict) -> list[str]:
        skills = [s for s in x.get("skills") or [] if s in SKILLS]
        if not skills:
            text = " ".join([x.get("subject", "")] + [t["name"] for t in x.get("topics", [])]
                            + [p for n in x.get("notes", []) for p in n.get("points", [])])
            if any(w in text.lower() for w in ("math", "algebra", "geometr", "angle", "fraction", "equation", "statistic")):
                skills = match_skills(text)
        return skills

    async def next_question(pid: str, rd: dict) -> dict:
        if rd["done"] >= ROUND:
            skills = await data.skills(pid)
            return {"round": sign(rd), "finished": True, "right": rd["right"], "total": rd["done"], "results": rd["results"],
                    "skills": [{"name": SKILLS[s].name, "level": round(skills.get(s, {}).get("level", 1.5), 1)} for s in rd["skills"]]}
        sid, level = pick_next(await data.skills(pid), rd["skills"], rd.get("last", ""))
        seed = secrets.randbits(48)
        q = make_question(sid, level, random.Random(seed))
        rd["last"] = sid
        qt = sign({"k": "q", "pid": pid, "skill": sid, "level": level, "seed": seed, "lesson": rd["lesson"],
                   "exp": time.time() + 6 * 3600})
        return {"round": sign(rd), "finished": False, "number": rd["done"] + 1, "of": ROUND,
                "question": {"id": qt, "skill": SKILLS[sid].name, "level": level, "text": q.text, "fields": q.fields, "diagram": q.diagram}}

    def round_of(token: str, pid: str) -> dict:
        rd = unsign(token)
        if not rd or rd.get("k") != "r" or rd.get("pid") != pid:
            raise StudyError("That round has expired. Start revising again.")
        return rd

    def question_of(token: str, pid: str):
        qt = unsign(token)
        if not qt or qt.get("k") != "q" or qt.get("pid") != pid or qt["skill"] not in SKILLS:
            raise StudyError("That question has expired. Start revising again.")
        return qt, make_question(qt["skill"], qt["level"], random.Random(qt["seed"]))

    @app.post("/study/api/revise/start")
    async def revise_start(request: Request, body: dict):
        p = await me(request)
        if body.get("lesson") == "all":
            skills = [x["skill"] for x in summarise(await data.skills(p["id"]))][:8]
            if not skills:
                raise StudyError("Revise a lesson first; then you can revise everything together.")
            lesson_id = "all"
        else:
            x = await load_lesson(p["id"], str(body.get("lesson", "")))
            skills, lesson_id = lesson_skills(x), x["id"]
            if not skills:
                return {"cards": True}
        rd = {"k": "r", "pid": p["id"], "lesson": lesson_id, "skills": skills, "done": 0, "right": 0, "results": [],
              "last": "", "exp": time.time() + 12 * 3600}
        return await next_question(p["id"], rd)

    @app.post("/study/api/revise/answer")
    async def revise_answer(request: Request, body: dict):
        p = await me(request)
        rd = round_of(body.get("round", ""), p["id"])
        qt, q = question_of(body.get("question", ""), p["id"])
        typed = (body.get("values") or []) + [""] * len(q.answers)
        right = all(check_field(a, t) for a, t in zip(q.answers, typed))
        if not right and int(body.get("tries") or 0) == 0:
            return {"right": False, "try_again": True}
        helped = bool(body.get("helped"))
        await data.s.put(f"u/{p['id']}/ans/{now_ms():013d}-{qt['skill']}-{qt['level']}-{int(right)}-{int(helped)}-{qt['lesson']}-{qt['seed']:x}")
        skills = await data.skills(p["id"])
        rd.update(done=rd["done"] + 1, right=rd["right"] + int(right), results=rd["results"] + [right])
        return {"right": right, "answer": [show_answer(a) for a in q.answers],
                "level": round(skills.get(qt["skill"], {}).get("level", 1.5), 1), "round": sign(rd)}

    @app.post("/study/api/revise/next")
    async def revise_next(request: Request, body: dict):
        p = await me(request)
        return await next_question(p["id"], round_of(body.get("round", ""), p["id"]))

    @app.post("/study/api/revise/help")
    async def revise_help(request: Request, body: dict):
        p = await me(request)
        qt, q = question_of(body.get("question", ""), p["id"])
        answer = ", ".join(f"{f.get('label') or ''} {show_answer(a)}{(' ' + f['suffix']) if f.get('suffix') else ''}".strip()
                           for a, f in zip(q.answers, q.fields))
        try:
            steps = [str(s).strip() for s in _parse_json(await claude(HELP_PROMPT.format(
                question=q.text, answer=answer, method=q.method or SKILLS[qt["skill"]].name), 900)).get("steps", []) if str(s).strip()]
        except Exception as e:  # noqa: BLE001
            log.warning("help failed: %s", e)
            steps = []
        return {"steps": steps or [q.method or "Have a look at your notes for this topic, then try again."]}

    @app.get("/study/api/progress")
    async def progress(request: Request):
        p = await me(request)
        answers = await data.answers(p["id"])
        return {"skills": summarise(await data.skills(p["id"])), "week": week(answers)}

    return app


app = create_app()
