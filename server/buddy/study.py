"""Study Buddy: lessons turned into notes and practice questions, one profile per person.

Each person has a name and a 4-digit PIN. A lesson comes in as a recording (any audio file the Mac
can read, e.g. a phone voice memo) or as pasted text. Recordings are turned into text on this
computer by Buddy's own ears, then Claude writes the notes, the topics covered and practice
questions. The recording itself is deleted once it's been turned into text.

Everything lives in data/study/: profiles.json, and a folder per person with one JSON file per lesson.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path

import numpy as np

from .audio import resample
from .study_text import CHECK_PROMPT, LESSON_PROMPT, StudyError, _clean_name, _hash_pin, _parse_json, tidy  # noqa: F401

log = logging.getLogger("buddy.study")

SR = 16000
CHUNK_MIN_S, CHUNK_MAX_S = 18, 28  # long recordings are cut at quiet moments into pieces this long
MAX_TRANSCRIPT_CHARS = 120_000  # about two hours of talking
MAX_UPLOAD_BYTES = 400 * 1024 * 1024
MIN_LESSON_S = 10  # anything 10 seconds or longer is kept, whatever it's about
INBOX_FOLDER = "Buddy lessons"  # voice memos put in a folder with this name on the Mac are picked up too
AUDIO_EXT = {".m4a", ".mp3", ".wav", ".aac", ".caf", ".aif", ".aiff", ".flac", ".ogg", ".mp4", ".qta"}

class StudyStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "study"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "profiles.json"
        self._lock = threading.Lock()
        self.data = {"profiles": [], "tokens": {}, "recorder_owner": "", "imported": {}}
        if self.path.exists():
            try:
                self.data.update(json.loads(self.path.read_text()))
            except (OSError, ValueError):
                log.warning("study profiles file unreadable; starting fresh")

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        tmp.replace(self.path)

    # ---------- profiles ----------
    def profiles(self) -> list[dict]:
        return [{"id": p["id"], "name": p["name"], "colour": p.get("colour", 0)} for p in self.data["profiles"]]

    def add_profile(self, name: str, pin: str) -> dict:
        name = _clean_name(name)
        if not name:
            raise StudyError("Type a name.")
        if not re.fullmatch(r"\d{4}", pin or ""):
            raise StudyError("The PIN needs to be 4 numbers.")
        with self._lock:
            if any(p["name"].lower() == name.lower() for p in self.data["profiles"]):
                raise StudyError("There's already someone with that name.")
            if len(self.data["profiles"]) >= 12:
                raise StudyError("That's the most people Study Buddy can hold.")
            salt = secrets.token_hex(8)
            p = {"id": secrets.token_hex(4), "name": name, "salt": salt, "pin": _hash_pin(pin, salt),
                 "colour": len(self.data["profiles"]) % 6, "created": time.time()}
            self.data["profiles"].append(p)
            self._save()
        (self.dir / p["id"] / "lessons").mkdir(parents=True, exist_ok=True)
        return {"id": p["id"], "name": p["name"], "colour": p["colour"]}

    def _profile(self, pid: str) -> dict:
        p = next((p for p in self.data["profiles"] if p["id"] == pid), None)
        if not p:
            raise StudyError("That person isn't here any more.")
        return p

    def login(self, pid: str, pin: str) -> str:
        p = self._profile(pid)
        fails = p.get("fails", 0)
        if fails >= 5 and time.time() - p.get("failed_at", 0) < 60:
            raise StudyError("Too many wrong tries. Wait a minute, then try again.")
        with self._lock:
            if _hash_pin(pin or "", p["salt"]) != p["pin"]:
                p["fails"], p["failed_at"] = fails + 1, time.time()
                self._save()
                raise StudyError("Wrong PIN.")
            p["fails"] = 0
            token = secrets.token_urlsafe(24)
            self.data["tokens"][token] = {"profile": pid, "at": time.time()}
            # Keep the 50 newest sign-ins.
            if len(self.data["tokens"]) > 50:
                for t, _ in sorted(self.data["tokens"].items(), key=lambda kv: kv[1]["at"])[:-50]:
                    self.data["tokens"].pop(t, None)
            self._save()
        return token

    def logout(self, token: str) -> None:
        with self._lock:
            if self.data["tokens"].pop(token or "", None):
                self._save()

    def who(self, token: str | None) -> dict | None:
        t = self.data["tokens"].get(token or "")
        if not t:
            return None
        p = next((p for p in self.data["profiles"] if p["id"] == t["profile"]), None)
        return {"id": p["id"], "name": p["name"], "colour": p.get("colour", 0)} if p else None

    def set_recorder_owner(self, pid: str) -> None:
        """Recordings from the pocket Buddy (and the "Buddy lessons" folder) go to this person."""
        with self._lock:
            self.data["recorder_owner"] = pid
            self._save()

    def recorder_owner(self) -> str:
        pid = self.data.get("recorder_owner", "")
        if any(p["id"] == pid for p in self.data["profiles"]):
            return pid
        return self.data["profiles"][0]["id"] if len(self.data["profiles"]) == 1 else ""

    def mark_imported(self, key: str) -> None:
        with self._lock:
            self.data.setdefault("imported", {})[key] = time.time()
            self._save()

    def remove_profile(self, pid: str) -> None:
        with self._lock:
            self.data["profiles"] = [p for p in self.data["profiles"] if p["id"] != pid]
            self.data["tokens"] = {t: v for t, v in self.data["tokens"].items() if v["profile"] != pid}
            self._save()
        shutil.rmtree(self.dir / pid, ignore_errors=True)

    # ---------- lessons ----------
    def _lessons_dir(self, pid: str) -> Path:
        d = self.dir / pid / "lessons"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _lesson_path(self, pid: str, lid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{8}", lid or ""):
            raise StudyError("No such lesson.")
        return self._lessons_dir(pid) / f"{lid}.json"

    def new_lesson(self, pid: str, subject: str, date: str, title: str, source: str) -> dict:
        lesson = {"id": secrets.token_hex(4), "subject": _clean_name(subject) or "", "date": date or time.strftime("%Y-%m-%d"),
                  "title": (title or "").strip()[:80], "source": source, "created": time.time(),
                  "status": "waiting", "progress": 0, "error": "", "transcript": "", "summary": "",
                  "notes": [], "topics": [], "questions": [], "duration_s": 0, "about": ""}
        self.save_lesson(pid, lesson)
        return lesson

    def save_lesson(self, pid: str, lesson: dict) -> None:
        p = self._lesson_path(pid, lesson["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(lesson, indent=1))
        tmp.replace(p)

    def lesson(self, pid: str, lid: str) -> dict:
        p = self._lesson_path(pid, lid)
        if not p.exists():
            raise StudyError("No such lesson.")
        return json.loads(p.read_text())

    def lessons(self, pid: str) -> list[dict]:
        out = []
        for f in self._lessons_dir(pid).glob("*.json"):
            try:
                x = json.loads(f.read_text())
            except (OSError, ValueError):
                continue
            out.append({k: x.get(k) for k in ("id", "title", "auto_title", "subject", "about", "date", "status", "progress", "error",
                                               "created", "duration_s")}
                       | {"topics": [t["name"] for t in x.get("topics", [])], "questions": len(x.get("questions", []))})
        return sorted(out, key=lambda x: (x["date"] or "", x["created"]), reverse=True)

    def delete_lesson(self, pid: str, lid: str) -> None:
        self._lesson_path(pid, lid).unlink(missing_ok=True)
        (self.dir / pid / "uploads" / lid).unlink(missing_ok=True)

    def upload_path(self, pid: str, lid: str) -> Path:
        d = self.dir / pid / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d / lid


# ---------- recordings waiting to become lessons ----------
class Recordings:
    """Each person's own list of recordings: from the pocket Buddy, or voice memos put in the
    "Buddy lessons" folder. Each one is checked (long enough, really a lesson) and turned into text
    by itself; then you pick one to make notes from."""

    def __init__(self, store: StudyStore):
        self.store = store

    def _dir(self, pid: str) -> Path:
        d = self.store.dir / pid / "recordings"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, pid: str, rid: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{8}", rid or ""):
            raise StudyError("No such recording.")
        return self._dir(pid) / f"{rid}.json"

    def audio_path(self, pid: str, rid: str, ext: str = "") -> Path:
        """The recording keeps its own file ending (.m4a, .wav...) so the Mac knows how to read it."""
        if not ext:
            found = [f for f in self._dir(pid).glob(f"{rid}.*") if f.suffix not in (".json", ".tmp")]
            return found[0] if found else self._dir(pid) / f"{rid}.wav"
        return self._dir(pid) / f"{rid}{ext}"

    def add(self, pid: str, src: Path, recorded_at: float, source: str, move: bool = False) -> dict:
        rec = {"id": secrets.token_hex(4), "recorded_at": recorded_at, "source": source, "status": "checking",
               "name": "", "subject": "", "about": "", "duration_s": 0, "transcript": "", "created": time.time(),
               "used_by": ""}
        ext = src.suffix.lower() if src.suffix.lower() in AUDIO_EXT else ".wav"
        dst = self.audio_path(pid, rec["id"], ext)
        (shutil.move if move else shutil.copyfile)(str(src), str(dst))
        self.save(pid, rec)
        return rec

    def save(self, pid: str, rec: dict) -> None:
        p = self._path(pid, rec["id"])
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(rec, indent=1))
        tmp.replace(p)

    def get(self, pid: str, rid: str) -> dict:
        p = self._path(pid, rid)
        if not p.exists():
            raise StudyError("No such recording.")
        return json.loads(p.read_text())

    def all(self, pid: str) -> list[dict]:
        out = []
        for f in self._dir(pid).glob("*.json"):
            try:
                out.append(json.loads(f.read_text()))
            except (OSError, ValueError):
                continue
        return sorted(out, key=lambda r: r["recorded_at"], reverse=True)

    def listed(self, pid: str) -> list[dict]:
        """What the "Choose recording" list shows: lessons being checked or ready, not yet used."""
        return [{k: r[k] for k in ("id", "recorded_at", "status", "name", "subject", "about", "duration_s", "source")}
                for r in self.all(pid) if r["status"] in ("checking", "ready") and not r["used_by"]]

    def delete(self, pid: str, rid: str) -> None:
        self._path(pid, rid).unlink(missing_ok=True)
        self.audio_path(pid, rid).unlink(missing_ok=True)

    def cleanup(self, pid: str, days: float = 7) -> None:
        """Recordings that weren't lessons are kept a week (so they're not picked up twice), then forgotten."""
        for r in self.all(pid):
            if r["status"] in ("too_short", "not_lesson", "unclear") and time.time() - r["created"] > days * 86400:
                self.delete(pid, r["id"])


def find_inbox_folder() -> Path | None:
    home = Path.home()
    for p in (home / "Desktop", home / "Documents", home / "Downloads", home):
        try:
            for child in p.iterdir():
                if child.is_dir() and child.name.strip().lower() == INBOX_FOLDER.lower():
                    return child
        except OSError:
            continue
    return None


# ---------- turning a recording into text ----------
def read_audio(path: Path) -> np.ndarray:
    """Any audio file the Mac (afconvert) or ffmpeg can read, as 16 kHz mono 16-bit."""
    wav = path.with_suffix(".wav16")
    try:
        if shutil.which("afconvert"):
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(path), str(wav)],
                           check=True, capture_output=True, timeout=600)
        elif shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(path), "-ac", "1", "-ar", "16000",
                            "-f", "wav", str(wav)], check=True, capture_output=True, timeout=600)
        else:
            wav = path  # only plain WAV files work without a converter
        with wave.open(str(wav)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(w.getnframes())
    except (subprocess.CalledProcessError, wave.Error, EOFError) as e:
        raise StudyError("Couldn't read that recording. Try a different file (voice memos, mp3 and m4a work).") from e
    finally:
        if wav != path:
            wav.unlink(missing_ok=True)
    if width != 2:
        raise StudyError("Couldn't read that recording (it isn't 16-bit audio).")
    x = np.frombuffer(raw, dtype="<i2")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype(np.int16)
    if rate != SR:
        x = np.clip(resample(x.astype(np.float64), rate, SR), -32768, 32767).astype(np.int16)
    return x


def split_at_pauses(x: np.ndarray) -> list[np.ndarray]:
    """Cut a long recording into pieces of 18-28 s, each cut at the quietest moment, so words
    aren't chopped in half."""
    frame = SR // 50  # 20 ms
    n = len(x) // frame
    if n == 0:
        return []
    rms = np.sqrt(np.mean(x[: n * frame].astype(np.float64).reshape(n, frame) ** 2, axis=1))
    pieces, start = [], 0
    lo, hi = CHUNK_MIN_S * 50, CHUNK_MAX_S * 50
    while n - start > hi:
        window = rms[start + lo:start + hi]
        cut = start + lo + int(np.argmin(window))
        pieces.append(x[start * frame:cut * frame])
        start = cut
    pieces.append(x[start * frame:])
    return pieces


class StudyWorker:
    """Turns new lessons into notes, one at a time, in the background."""

    def __init__(self, store: StudyStore, stt, llm):
        self.store, self.stt, self.llm = store, stt, llm
        self.recordings = Recordings(store)
        self.cloud = lambda: None  # (website address, sync key) once Buddy is connected to Study Buddy online
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None
        self.watch_task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        if self.watch_task is None:
            self.watch_task = asyncio.create_task(self._watch_folder())
        # Lessons that were half done when Buddy last stopped: start them again.
        for p in self.store.profiles():
            for x in self.store.lessons(p["id"]):
                if x["status"] in ("waiting", "listening", "writing"):
                    self.queue.put_nowait((p["id"], x["id"]))
        for p in self.store.profiles() + [{"id": "cloud"}]:
            for r in self.recordings.all(p["id"]):
                if r["status"] == "checking":
                    self.queue.put_nowait((p["id"], "rec:" + r["id"]))

    def add(self, pid: str, lid: str) -> None:
        self.queue.put_nowait((pid, lid))

    def add_recording(self, pid: str, src: Path, recorded_at: float, source: str, move: bool = False) -> dict:
        rec = self.recordings.add(pid, src, recorded_at, source, move)
        self.queue.put_nowait((pid, "rec:" + rec["id"]))
        return rec

    async def _watch_folder(self) -> None:
        """Voice memos put in the "Buddy lessons" folder go into the recorder owner's list by themselves."""
        while True:
            try:
                await asyncio.to_thread(self.scan_folder)
                await self.push_waiting()
            except Exception:  # noqa: BLE001 - keep watching
                log.exception("lesson folder scan failed")
            await asyncio.sleep(30)

    def owner(self) -> str:
        """Who new recordings belong to: the website (once connected), or a person on this Mac."""
        return "cloud" if self.cloud() else self.store.recorder_owner()

    async def push_waiting(self) -> None:
        """Send checked lessons that haven't reached the website yet (it was offline, say)."""
        if not self.cloud():
            return
        for r in self.recordings.all("cloud"):
            if r["status"] == "ready" and not r.get("sent"):
                await self.push("cloud", r)

    async def push(self, pid: str, rec: dict) -> bool:
        cloud = self.cloud()
        if not cloud:
            return False
        url, key = cloud
        import httpx
        body = {k: rec.get(k) for k in ("id", "recorded_at", "name", "subject", "about", "duration_s", "transcript", "source")}
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(f"{url.rstrip('/')}/study/api/sync/recording", json=body, headers={"X-Sync-Key": key})
            ok = r.status_code == 200
            if not ok:
                log.warning("study website refused a recording: %s %s", r.status_code, r.text[:200])
        except Exception as e:  # noqa: BLE001 - try again later
            log.warning("couldn't reach the study website: %s", e)
            ok = False
        if ok:
            rec["sent"] = True
            self.recordings.save(pid, rec)
        return ok

    def scan_folder(self, folder: Path | None = None) -> int:
        folder = folder or find_inbox_folder()
        pid = self.owner()
        if not folder or not pid:
            return 0
        n = 0
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in AUDIO_EXT or f.name.startswith("."):
                continue
            st = f.stat()
            key = f"{f.name}|{st.st_size}|{int(st.st_mtime)}"
            if key in self.store.data.get("imported", {}) or time.time() - st.st_mtime < 10:
                continue  # already taken, or still being copied in
            self.store.mark_imported(key)
            self.add_recording(pid, f, st.st_mtime, "folder")
            n += 1
        for p in self.store.profiles() + [{"id": "cloud"}]:
            self.recordings.cleanup(p["id"])
        return n

    async def _run(self) -> None:
        while True:
            pid, lid = await self.queue.get()
            try:
                if lid.startswith("rec:"):
                    try:
                        await self.check_recording(pid, lid[4:])
                    except Exception:  # noqa: BLE001 - Claude down, say: check it again in 5 minutes
                        log.exception("checking a recording failed; trying again later")
                        asyncio.get_running_loop().call_later(300, self.queue.put_nowait, (pid, lid))
                    continue
                await self.process(pid, lid)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - one bad lesson mustn't stop the rest
                log.exception("study lesson failed")
                try:
                    lesson = self.store.lesson(pid, lid)
                    lesson.update(status="failed", error=str(e) if isinstance(e, StudyError) else "Something went wrong making the notes.")
                    self.store.save_lesson(pid, lesson)
                except StudyError:
                    pass

    async def _transcribe_file(self, path: Path) -> tuple[str, int]:
        if not self.stt.available():
            raise StudyError("Buddy's ears aren't installed yet: click Update Buddy, then try again.")
        audio = await asyncio.to_thread(read_audio, path)
        words = []
        for piece in split_at_pauses(audio):
            text = await self.stt.transcribe(piece, "", len(piece) / SR)
            if text.strip():
                words.append(text.strip())
        return " ".join(words)[:MAX_TRANSCRIPT_CHARS], int(len(audio) / SR)

    async def check_recording(self, pid: str, rid: str) -> None:
        """10 seconds or longer? Then it goes in the list, named after its subject and day."""
        try:
            rec = self.recordings.get(pid, rid)
        except StudyError:
            return
        audio_file = self.recordings.audio_path(pid, rid)
        try:
            if not audio_file.exists():
                rec["status"] = "unclear"
                return
            seconds = await asyncio.to_thread(lambda: len(read_audio(audio_file)) / SR)
            rec["duration_s"] = int(seconds)
            if seconds < MIN_LESSON_S:
                rec["status"] = "too_short"
                return
            rec["transcript"], rec["duration_s"] = await self._transcribe_file(audio_file)
            sample = " ".join(rec["transcript"].split()[:2500])
            verdict = {}
            if sample:
                try:  # just names it; every recording is kept
                    r = await self.llm.chat([{"role": "user", "content": CHECK_PROMPT.format(text=sample)}],
                                            tier="fast", json_mode=True, max_tokens=300)
                    verdict = _parse_json(r.content)
                except Exception:
                    verdict = {}
            rec["subject"] = _clean_name(str(verdict.get("subject", "")))
            rec["about"] = str(verdict.get("about", "")).strip().rstrip(".")[:80] or (
                "" if sample else "couldn't make out any words")
            rec["name"] = f"{rec['subject']} lesson" if rec["subject"] else "Lesson"
            rec["status"] = "ready"
        except StudyError as e:
            rec["status"], rec["error"] = "unclear", str(e)
        finally:
            if rec["status"] != "checking":
                audio_file.unlink(missing_ok=True)  # the words are kept; the recording isn't
            self.recordings.save(pid, rec)
        if rec["status"] == "ready" and pid == "cloud":
            await self.push(pid, rec)  # off to Study Buddy online

    def lesson_from_recording(self, pid: str, rid: str, title: str = "") -> dict:
        rec = self.recordings.get(pid, rid)
        if rec["status"] != "ready":
            raise StudyError("That recording is still being checked.")
        if rec["used_by"]:
            raise StudyError("Notes have already been made from that recording.")
        day = time.strftime("%Y-%m-%d", time.localtime(rec["recorded_at"]))
        lesson = self.store.new_lesson(pid, rec["subject"], day, title, "audio")
        lesson.update(transcript=rec["transcript"], duration_s=rec["duration_s"], about=rec["about"])
        self.store.save_lesson(pid, lesson)
        rec["used_by"] = lesson["id"]
        self.recordings.save(pid, rec)
        self.add(pid, lesson["id"])
        return lesson

    async def process(self, pid: str, lid: str) -> None:
        lesson = self.store.lesson(pid, lid)
        upload = self.store.upload_path(pid, lid)
        if lesson["source"] == "audio" and not lesson["transcript"]:
            if not upload.exists():
                raise StudyError("The recording went missing. Try uploading it again.")
            if not self.stt.available():
                raise StudyError("Buddy's ears aren't installed yet: click Update Buddy, then try again.")
            lesson.update(status="listening", progress=0, error="")
            self.store.save_lesson(pid, lesson)
            audio = await asyncio.to_thread(read_audio, upload)
            lesson["duration_s"] = int(len(audio) / SR)
            pieces = split_at_pauses(audio)
            words = []
            for i, piece in enumerate(pieces):
                text = await self.stt.transcribe(piece, "", len(piece) / SR)
                if text.strip():
                    words.append(text.strip())
                lesson["progress"] = int(100 * (i + 1) / len(pieces))
                self.store.save_lesson(pid, lesson)
            lesson["transcript"] = " ".join(words)[:MAX_TRANSCRIPT_CHARS]
            upload.unlink(missing_ok=True)  # the words are kept; the recording isn't
            if len(lesson["transcript"].split()) < 15:
                raise StudyError("Couldn't make out enough words in that recording. Was the mic close enough?")
        lesson.update(status="writing", progress=0)
        self.store.save_lesson(pid, lesson)
        what = "a transcript of a recording" if lesson["source"] == "audio" else "notes or text"
        from .practice import catalogue_text
        prompt = LESSON_PROMPT.format(skills=catalogue_text(), what=what, what_cap="Transcript" if lesson["source"] == "audio" else "Text",
                                      subject=lesson["subject"] or "school", text=lesson["transcript"])
        r = await self.llm.chat([{"role": "user", "content": prompt}], tier="fast", json_mode=True, max_tokens=6000)
        tidy(_parse_json(r.content), lesson)
        if not lesson["notes"] and not lesson["questions"]:
            raise StudyError("There wasn't enough lesson in there to make notes from.")
        lesson.update(status="ready", progress=100, error="")
        self.store.save_lesson(pid, lesson)
