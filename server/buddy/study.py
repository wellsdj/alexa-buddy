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

log = logging.getLogger("buddy.study")

SR = 16000
CHUNK_MIN_S, CHUNK_MAX_S = 18, 28  # long recordings are cut at quiet moments into pieces this long
MAX_TRANSCRIPT_CHARS = 120_000  # about two hours of talking
MAX_UPLOAD_BYTES = 400 * 1024 * 1024

LESSON_PROMPT = """You help a secondary-school student revise. Below is {what} from a {subject} lesson.
It may be messy: several people talking, gaps and mistakes from automatic transcription. Work out what
was actually taught, and ignore chat that isn't about the lesson.

Reply with JSON only, in exactly this shape:
{{
  "title": "short title for the lesson, 3-7 words",
  "subject": "the school subject, one or two words, e.g. Maths, English, Chemistry",
  "about": "what the lesson was about in under 8 words, starting with a small letter, e.g. calculating angles in triangles",
  "summary": "2-3 plain sentences: what the lesson was about",
  "notes": [{{"heading": "...", "points": ["clear revision note", "..."]}}],
  "topics": [{{"name": "short topic name", "summary": "one sentence"}}],
  "questions": [{{"topic": "one of the topic names", "level": 1, "question": "...",
                  "answer": "the correct answer, short", "why": "one or two sentences explaining it",
                  "choices": []}}]
}}

Rules:
- Notes: the key facts, methods, definitions and the teacher's worked examples, in simple language.
  Keep any numbers, formulas and steps exactly. 3-8 headings.
- Topics: 2-8 topics, named the way a teacher or exam board would (e.g. "Simultaneous equations").
- Questions: 8-15 practice questions covering every topic, from easy (level 1) through medium (2) to
  exam-style (3). Make up fresh numbers rather than repeating the lesson's examples word for word.
  For about a third of them give 4 "choices" (multiple choice, the answer must be one of them);
  leave "choices" empty for the rest.
- Don't invent content that wasn't in the lesson. If there's too little to work with, still reply in
  this shape, with fewer notes and questions.

{what_cap}:
\"\"\"
{text}
\"\"\""""


class StudyError(Exception):
    pass


def _hash_pin(pin: str, salt: str) -> str:
    return hashlib.sha256(f"{salt}:{pin}".encode()).hexdigest()


def _clean_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip())[:30]


class StudyStore:
    def __init__(self, data_dir: Path):
        self.dir = data_dir / "study"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "profiles.json"
        self._lock = threading.Lock()
        self.data = {"profiles": [], "tokens": {}}
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


def _parse_json(text: str) -> dict:
    text = text.strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise StudyError("Claude didn't send back notes.")
    try:
        return json.loads(m.group(0))
    except ValueError as e:
        raise StudyError("Claude's notes came back garbled.") from e


def tidy(result: dict, lesson: dict) -> dict:
    """Keep only well-formed notes, topics and questions."""
    notes = [{"heading": str(n.get("heading", "")).strip(), "points": [str(p).strip() for p in n.get("points", []) if str(p).strip()]}
             for n in result.get("notes", []) if isinstance(n, dict)]
    topics = [{"name": str(t.get("name", "")).strip(), "summary": str(t.get("summary", "")).strip()}
              for t in result.get("topics", []) if isinstance(t, dict) and str(t.get("name", "")).strip()]
    questions = []
    for i, q in enumerate(result.get("questions", [])):
        if not isinstance(q, dict) or not str(q.get("question", "")).strip() or not str(q.get("answer", "")).strip():
            continue
        choices = [str(c).strip() for c in q.get("choices") or [] if str(c).strip()]
        answer = str(q["answer"]).strip()
        if choices and answer not in choices:
            choices = []  # a multiple-choice question whose answer isn't a choice: ask it as a normal one
        try:
            level = min(3, max(1, int(q.get("level", 2))))
        except (TypeError, ValueError):
            level = 2
        questions.append({"id": f"q{i + 1}", "topic": str(q.get("topic", "")).strip(), "level": level,
                          "question": str(q["question"]).strip(), "answer": answer,
                          "why": str(q.get("why", "")).strip(), "choices": choices})
    # Your own title stays; Buddy's suggestion is kept apart, shown only if you didn't give one.
    lesson.update({"auto_title": str(result.get("title", "")).strip()[:80],
                   "subject": lesson.get("subject") or str(result.get("subject", "")).strip()[:30],
                   "summary": str(result.get("summary", "")).strip(),
                   "about": str(result.get("about", "")).strip().rstrip(".")[:80],
                   "notes": [n for n in notes if n["heading"] or n["points"]], "topics": topics,
                   "questions": questions})
    return lesson


class StudyWorker:
    """Turns new lessons into notes, one at a time, in the background."""

    def __init__(self, store: StudyStore, stt, llm):
        self.store, self.stt, self.llm = store, stt, llm
        self.queue: asyncio.Queue = asyncio.Queue()
        self.task: asyncio.Task | None = None

    def start(self) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self._run())
        # Lessons that were half done when Buddy last stopped: start them again.
        for p in self.store.profiles():
            for x in self.store.lessons(p["id"]):
                if x["status"] in ("waiting", "listening", "writing"):
                    self.queue.put_nowait((p["id"], x["id"]))

    def add(self, pid: str, lid: str) -> None:
        self.queue.put_nowait((pid, lid))

    async def _run(self) -> None:
        while True:
            pid, lid = await self.queue.get()
            try:
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
        prompt = LESSON_PROMPT.format(what=what, what_cap="Transcript" if lesson["source"] == "audio" else "Text",
                                      subject=lesson["subject"] or "school", text=lesson["transcript"])
        r = await self.llm.chat([{"role": "user", "content": prompt}], tier="fast", json_mode=True, max_tokens=6000)
        tidy(_parse_json(r.content), lesson)
        if not lesson["notes"] and not lesson["questions"]:
            raise StudyError("There wasn't enough lesson in there to make notes from.")
        lesson.update(status="ready", progress=100, error="")
        self.store.save_lesson(pid, lesson)
