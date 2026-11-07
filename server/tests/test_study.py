"""Study Buddy: profiles with PINs, lessons in, notes and questions out."""
import asyncio
import json
import wave
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from buddy.main import create_app
from buddy.study import StudyError, StudyStore, StudyWorker, split_at_pauses, tidy

NOTES = {
    "title": "Solving simultaneous equations", "subject": "Maths",
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
    assert lesson["title"] == "Solving simultaneous equations"


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
    assert done["title"] == "Solving simultaneous equations" and done["subject"] == "Maths"
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
