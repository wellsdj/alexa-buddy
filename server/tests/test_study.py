"""Study Buddy: profiles with PINs, lessons in, notes and questions out."""
import asyncio
import time
import json
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from buddy.main import create_app
from buddy.study import StudyError, StudyStore, StudyWorker, split_at_pauses, tidy

NOTES = {
    "title": "Solving simultaneous equations", "subject": "Maths", "about": "solving two equations at once.",
    "summary": "How to solve two equations together by elimination.",
    "notes": [{"heading": "Elimination", "points": ["Make one variable's numbers match", "Add or subtract"]}],
    "topics": [{"name": "Simultaneous equations", "summary": "Two equations, two unknowns."}],
    "questions": [
        {"topic": "Simultaneous equations", "level": 1, "question": "x + y = 5, x - y = 1. Find x.",
         "answer": "3", "why": "Add them: 2x = 6.", "choices": ["2", "3", "4", "5"]},
        {"topic": "Simultaneous equations", "level": 3, "question": "Solve 2x + 3y = 12 and x - y = 1.",
         "answer": "x = 3, y = 2", "why": "Elimination.", "choices": []},
        {"topic": "x", "question": "", "answer": "nothing"},  # broken: dropped
    ],
}
LESSON_TEXT = ("Today we're doing simultaneous equations. When you have two equations with two unknowns "
               "you can add or subtract them to get rid of one letter. That's called elimination.")


class FakeLLM:
    def __init__(self, reply=NOTES):
        self.reply, self.prompts = reply, []

    async def chat(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        return SimpleNamespace(content="```json\n" + json.dumps(self.reply) + "\n```")


class FakeSTT:
    def __init__(self):
        self.pieces = []

    def available(self):
        return True

    async def transcribe(self, pcm, prompt="", speech_seconds=1.0):
        self.pieces.append(len(pcm) / 16000)
        return "we are learning about simultaneous equations and elimination today class"


def test_pins_keep_people_apart(tmp_path):
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")
    b = s.add_profile("Sam", "9999")
    with pytest.raises(StudyError):
        s.login(a["id"], "9999")
    with pytest.raises(StudyError):
        s.add_profile("alex", "1111")  # same name
    with pytest.raises(StudyError):
        s.add_profile("Jo", "12")  # PIN too short
    token = s.login(b["id"], "9999")
    assert s.who(token)["name"] == "Sam"
    lesson = s.new_lesson(a["id"], "Maths", "", "", "text")
    assert s.lessons(b["id"]) == [] and len(s.lessons(a["id"])) == 1
    with pytest.raises(StudyError):
        s.lesson(b["id"], lesson["id"])  # Sam can't open Alex's lesson
    assert StudyStore(tmp_path).who(token)["name"] == "Sam"  # still signed in after a restart


def test_too_many_wrong_pins_waits_a_minute(tmp_path):
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")
    for _ in range(5):
        with pytest.raises(StudyError):
            s.login(a["id"], "0000")
    with pytest.raises(StudyError, match="Wait a minute"):
        s.login(a["id"], "1234")


def test_long_recording_is_cut_at_quiet_moments():
    rng = np.random.default_rng(1)
    talk = lambda s: (rng.normal(0, 3000, int(s * 16000))).astype(np.int16)
    quiet = lambda s: np.zeros(int(s * 16000), np.int16)
    x = np.concatenate([talk(21), quiet(0.5), talk(22), quiet(0.5), talk(10)])
    pieces = split_at_pauses(x)
    assert sum(len(p) for p in pieces) == len(x) // 320 * 320
    assert all(len(p) / 16000 <= 28.5 for p in pieces)
    assert abs(len(pieces[0]) / 16000 - 21.25) < 0.6  # cut inside the pause


def test_tidy_drops_broken_questions_and_bad_choices():
    lesson = tidy(dict(NOTES, questions=NOTES["questions"] + [
        {"topic": "t", "question": "q?", "answer": "7", "choices": ["1", "2"]}]), {"title": "", "subject": ""})
    assert len(lesson["questions"]) == 3
    assert lesson["questions"][-1]["choices"] == []  # answer wasn't a choice: asked as a normal question
    assert lesson["auto_title"] == "Solving simultaneous equations" and lesson["title"] == ""


def test_recording_becomes_notes_and_is_then_deleted(tmp_path):
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")
    lesson = s.new_lesson(a["id"], "Maths", "", "", "audio")
    up = s.upload_path(a["id"], lesson["id"])
    with wave.open(str(up), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.random.default_rng(0).normal(0, 2000, 16000 * 60).astype(np.int16).tobytes())
    stt, llm = FakeSTT(), FakeLLM()
    asyncio.run(StudyWorker(s, stt, llm).process(a["id"], lesson["id"]))
    done = s.lesson(a["id"], lesson["id"])
    assert done["status"] == "ready" and done["duration_s"] == 60
    assert len(stt.pieces) >= 3 and done["notes"] and len(done["questions"]) == 2
    assert done["subject"] == "Maths" and done["about"] == "solving two equations at once"
    assert "transcript of a recording" in llm.prompts[0]
    assert not up.exists()


def test_website_flow(settings, monkeypatch):
    app = create_app(settings)
    llm = FakeLLM()
    with TestClient(app) as c:
        app.state.hub.study_worker.llm = llm
        assert c.get("/study").status_code == 200
        r = c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"}).json()
        h = {"X-Study-Token": r["token"]}
        assert c.get("/study/api/lessons").status_code == 401  # not signed in
        assert c.post("/study/api/lessons/text", headers=h, json={"text": "too short"}).status_code == 400
        lesson = c.post("/study/api/lessons/text", headers=h,
                        json={"subject": "Maths", "date": "2026-11-09", "text": LESSON_TEXT}).json()
        for _ in range(50):
            x = c.get(f"/study/api/lessons/{lesson['id']}", headers=h).json()
            if x["status"] == "ready":
                break
            import time
            time.sleep(0.05)
        assert x["status"] == "ready" and x["questions"][0]["choices"]
        listed = c.get("/study/api/lessons", headers=h).json()["lessons"]
        assert listed[0]["topics"] == ["Simultaneous equations"]
        sam = c.post("/study/api/profiles", json={"name": "Sam", "pin": "4321"}).json()
        assert c.get(f"/study/api/lessons/{lesson['id']}", headers={"X-Study-Token": sam["token"]}).status_code == 400
        assert c.delete(f"/study/api/lessons/{lesson['id']}", headers=h).json()["ok"]
        assert c.get("/study/api/lessons", headers=h).json()["lessons"] == []


# ---------- recordings that arrive by themselves ----------
class WordySTT(FakeSTT):
    def __init__(self, words=40):
        super().__init__()
        self.words = words

    async def transcribe(self, pcm, prompt="", speech_seconds=1.0):
        self.pieces.append(len(pcm) / 16000)
        return " ".join(["angles"] * self.words)


class CheckLLM(FakeLLM):
    def __init__(self, is_lesson=True):
        super().__init__()
        self.is_lesson = is_lesson

    async def chat(self, messages, **kw):
        self.prompts.append(messages[0]["content"])
        if "Give it a name" in messages[0]["content"]:
            return SimpleNamespace(content=json.dumps({"subject": "Maths",
                                                       "about": "calculating angles in triangles."}))
        return SimpleNamespace(content=json.dumps(NOTES))


def make_wav(path, seconds, mtime=None):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
        w.writeframes(np.random.default_rng(0).normal(0, 2000, int(16000 * seconds)).astype(np.int16).tobytes())
    if mtime:
        import os
        os.utime(path, (mtime, mtime))
    return path


def check(tmp_path, seconds, stt, llm):
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")
    w = StudyWorker(s, stt, llm)
    rec = w.recordings.add(a["id"], make_wav(tmp_path / "x.wav", seconds), 1794000000, "pocket")
    asyncio.run(w.check_recording(a["id"], rec["id"]))
    return w, a["id"], w.recordings.get(a["id"], rec["id"])


def test_recordings_under_10_seconds_are_dropped(tmp_path):
    w, pid, rec = check(tmp_path, 8, WordySTT(), CheckLLM())
    assert rec["status"] == "too_short" and w.recordings.listed(pid) == []


def test_anything_over_10_seconds_is_kept(tmp_path):
    w, pid, rec = check(tmp_path, 12, WordySTT(words=2), CheckLLM())
    assert rec["status"] == "ready" and len(w.recordings.listed(pid)) == 1


def test_silent_recording_is_still_kept(tmp_path):
    w, pid, rec = check(tmp_path, 12, WordySTT(words=0), CheckLLM())
    assert rec["status"] == "ready" and w.recordings.listed(pid)[0]["about"] == "couldn't make out any words"


def test_real_lesson_is_named_and_listed_then_becomes_notes(tmp_path):
    w, pid, rec = check(tmp_path, 150, WordySTT(), CheckLLM())
    listed = w.recordings.listed(pid)
    assert listed[0]["name"] == "Maths lesson" and listed[0]["about"] == "calculating angles in triangles"
    assert not w.recordings.audio_path(pid, rec["id"]).exists()  # words kept, recording deleted
    lesson = w.lesson_from_recording(pid, rec["id"])
    assert lesson["date"] == time.strftime("%Y-%m-%d", time.localtime(1794000000))
    asyncio.run(w.process(pid, lesson["id"]))
    assert w.store.lesson(pid, lesson["id"])["status"] == "ready"
    assert w.recordings.listed(pid) == []  # used: gone from the list


def test_voice_memos_in_the_folder_are_picked_up_once(tmp_path):
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")  # the only person, so recordings go to them
    w = StudyWorker(s, WordySTT(), CheckLLM())
    folder = tmp_path / "Buddy lessons"
    folder.mkdir()
    make_wav(folder / "Maths.wav", 5, mtime=time.time() - 60)
    (folder / "notes.txt").write_text("not audio")
    assert w.scan_folder(folder) == 1
    assert w.scan_folder(folder) == 0
    assert (folder / "Maths.wav").exists()  # your own file is left alone
    assert len(w.recordings.all(a["id"])) == 1


def test_pocket_buddy_can_send_a_recording(settings):
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"}).json()
        import io
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(16000)
            w.writeframes(np.zeros(16000 * 3, np.int16).tobytes())
        got = c.post("/study/api/device/recording?recorded_at=1794000000", content=buf.getvalue())
        assert got.status_code == 200
        assert c.get("/study/api/recordings", headers={"X-Study-Token": r["token"]}).json()["mine"]


def test_start_revising_flow_and_buddy_knows_progress(settings):
    from buddy.practice import show_answer
    app = create_app(settings)
    hub = app.state.hub

    class LLM(FakeLLM):
        async def chat(self, messages, **kw):
            if "maths tutor" in messages[0]["content"]:
                return SimpleNamespace(content=json.dumps({"steps": ["Take 3 from both sides.", "Divide by 2: x = 4."]}))
            return SimpleNamespace(content=json.dumps(dict(NOTES, skills=["linear_equations", "made_up_skill"])))

    with TestClient(app) as c:
        hub.study_worker.llm = hub.llm = LLM()
        tok = c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"}).json()["token"]
        h = {"X-Study-Token": tok}
        lesson = c.post("/study/api/lessons/text", headers=h, json={"subject": "Maths", "text": LESSON_TEXT}).json()
        for _ in range(60):
            if c.get(f"/study/api/lessons/{lesson['id']}", headers=h).json()["status"] == "ready":
                break
            time.sleep(0.05)
        assert c.get(f"/study/api/lessons/{lesson['id']}", headers=h).json()["skills"] == ["linear_equations"]
        r = c.post("/study/api/revise/start", headers=h, json={"lesson": lesson["id"]}).json()
        q = r["question"]
        assert q["skill"] == "Solving equations" and "answer" not in json.dumps(q)  # the answer stays on the server
        steps = c.post("/study/api/revise/help", headers=h, json={"question": q["id"]}).json()["steps"]
        assert len(steps) == 2
        right = [show_answer(a) for a in hub.practice.live[q["id"]].q.answers]
        got = c.post("/study/api/revise/answer", headers=h, json={"round": r["round"], "question": q["id"], "values": right}).json()
        assert got["right"]
        nxt = c.post("/study/api/revise/next", headers=h, json={"round": r["round"]}).json()
        assert nxt["number"] == 2
        prog = c.get("/study/api/progress", headers=h).json()
        assert prog["skills"][0]["seen"] == 1 and prog["week"]["right"] == 1
        said = hub.brain._study_progress()
        assert "Alex" in said and "Solving equations" in said and "1/1 right" in said


def test_once_connected_recordings_go_to_the_website(settings, monkeypatch):
    app = create_app(settings)
    hub = app.state.hub
    sent = []

    async def fake_push(pid, rec):
        sent.append((pid, rec["name"]))
        return True
    with TestClient(app, follow_redirects=False) as c:
        assert c.get("/api/study/cloud").json()["connected"] is False
        settings.set_secret("STUDY_SYNC_KEY", "k")
        assert hub.study_worker.owner() == "cloud"
        assert c.get("/study").headers["location"] == "https://studdy-buddy-uk.vercel.app"
        assert c.get("/study?local=1").status_code == 200
        monkeypatch.setattr(hub.study_worker, "push", fake_push)
        w = hub.study_worker
        w.stt, w.llm = WordySTT(), CheckLLM()
        rec = w.recordings.add("cloud", make_wav(settings.data_dir / "x.wav", 150), 1794000000, "pocket")
        asyncio.run(w.check_recording("cloud", rec["id"]))
        assert sent == [("cloud", "Maths lesson")]


def test_audio_still_reads_when_the_macs_converter_fails(tmp_path, monkeypatch):
    """afconvert can refuse a file (seen with an m4a on the Mac): fall back to the next way to read it."""
    import shutil as sh
    import subprocess as sp
    import wave as wv
    from buddy import study as st
    if not sh.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    src = tmp_path / "talk.wav"
    with wv.open(str(src), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(44100)
        w.writeframes((np.sin(np.arange(44100) / 10) * 8000).astype("<i2").tobytes())
    real_which, real_run, tried = sh.which, sp.run, []
    monkeypatch.setattr(st.shutil, "which", lambda name: "/usr/bin/afconvert" if name == "afconvert" else real_which(name))

    def run(cmd, **kw):
        tried.append(cmd[0])
        if cmd[0] == "afconvert":
            raise sp.CalledProcessError(1, cmd, stderr=b"Error: couldn't convert")
        return real_run(cmd, **kw)

    monkeypatch.setattr(st.subprocess, "run", run)
    x = st.read_audio(src)
    assert tried == ["afconvert", "afconvert", "ffmpeg"] and abs(len(x) - 16000) < 50
    assert list(tmp_path.iterdir()) == [src]  # nothing left behind


def test_unreadable_audio_says_what_went_wrong(tmp_path):
    from buddy import study as st
    bad = tmp_path / "broken.m4a"
    bad.write_bytes(b"not audio at all" * 100)
    with pytest.raises(st.StudyError) as e:
        st.read_audio(bad)
    assert "What went wrong" in str(e.value)
