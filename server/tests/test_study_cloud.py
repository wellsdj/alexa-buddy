"""Study Buddy online (api/index.py), with in-memory storage and a pretend Claude."""
import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from buddy.practice import make_question, show_answer
from tests.test_study import NOTES

SPEC = importlib.util.spec_from_file_location("study_cloud", Path(__file__).resolve().parents[1] / "api" / "index.py")


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setenv("STUDY_SECRET", "test-secret")
    monkeypatch.setenv("STUDY_SYNC_KEY", "sync-key")
    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    mod = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(mod)

    async def fake_claude(prompt, max_tokens=6000):
        if "maths tutor" in prompt:
            return json.dumps({"steps": ["One.", "Two."]})
        return json.dumps(dict(NOTES, skills=["linear_equations"]))
    monkeypatch.setattr(mod, "claude", fake_claude)
    return mod


def test_everything_online(cloud):
    import random
    c = TestClient(cloud.create_app(cloud.MemoryStore()))
    s = {"X-Sync-Key": "sync-key"}
    # Buddy's Mac can't send recordings until someone exists
    rec = {"id": "aaaabbbb", "recorded_at": 1794000000, "name": "Maths lesson", "subject": "Maths",
           "about": "solving equations", "duration_s": 2900, "transcript": "we solve equations " * 50}
    assert c.post("/study/api/sync/recording", json=rec, headers=s).status_code == 409
    assert c.post("/study/api/sync/recording", json=rec).status_code == 401  # no key
    tok = c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"}).json()["token"]
    h = {"X-Study-Token": tok}
    assert c.post("/study/api/sync/recording", json=rec, headers=s).json()["ok"]
    assert c.post("/study/api/sync/recording", json=rec, headers=s).json()["ok"]  # sent twice: kept once
    recs = c.get("/study/api/recordings", headers=h).json()
    assert len(recs["recordings"]) == 1 and recs["mine"] and recs["can_upload"] is False
    lesson = c.post("/study/api/recordings/aaaabbbb/use", headers=h, json={"date": "2026-11-05"}).json()
    assert c.get("/study/api/recordings", headers=h).json()["recordings"] == []
    got = c.get(f"/study/api/lessons/{lesson['id']}", headers=h).json()  # opening it writes the notes
    assert got["status"] == "ready" and got["skills"] == ["linear_equations"] and got["date"] == "2026-11-05"
    listed = c.get("/study/api/lessons", headers=h).json()["lessons"]
    assert listed[0]["about"] == "solving two equations at once"
    # revising: tokens carry the question; the server works the answer out again to mark it
    r = c.post("/study/api/revise/start", headers=h, json={"lesson": lesson["id"]}).json()
    q = r["question"]
    tok_q = cloud.unsign(q["id"])
    right = [show_answer(a) for a in make_question(tok_q["skill"], tok_q["level"], random.Random(tok_q["seed"])).answers]
    assert c.post("/study/api/revise/answer", headers=h, json={"round": r["round"], "question": q["id"], "values": ["zzz"]}).json()["try_again"]
    a = c.post("/study/api/revise/answer", headers=h, json={"round": r["round"], "question": q["id"], "values": right, "tries": 1}).json()
    assert a["right"] and a["level"] > 1.5
    nxt = c.post("/study/api/revise/next", headers=h, json={"round": a["round"]}).json()
    assert nxt["number"] == 2
    assert c.post("/study/api/revise/help", headers=h, json={"question": nxt["question"]["id"]}).json()["steps"] == ["One.", "Two."]
    prog = c.get("/study/api/progress", headers=h).json()
    assert prog["skills"][0]["right"] == 1 and prog["week"]["answered"] == 1
    said = c.get("/study/api/sync/progress?name=Alex", headers=s).json()["text"]
    assert "Solving equations: level" in said and "1 lessons" in said
    # someone else can't see Alex's lesson, and a forged token doesn't work
    sam = c.post("/study/api/profiles", json={"name": "Sam", "pin": "4321"}).json()["token"]
    assert c.get(f"/study/api/lessons/{lesson['id']}", headers={"X-Study-Token": sam}).status_code == 400
    assert c.get("/study/api/lessons", headers={"X-Study-Token": tok[:-2] + "00"}).status_code == 401


def test_pairing_buddy_with_a_code(cloud):
    c = TestClient(cloud.create_app(cloud.MemoryStore()))
    c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"})
    sam = c.post("/study/api/profiles", json={"name": "Sam", "pin": "4321"}).json()
    code = c.post("/study/api/pair-code", headers={"X-Study-Token": sam["token"]}).json()["code"]
    assert c.post("/study/api/sync/pair", json={"code": "WRONG1"}).status_code == 400
    assert c.post("/study/api/sync/pair", json={"code": code.lower()}).json()["key"] == "sync-key"
    assert c.post("/study/api/sync/pair", json={"code": code}).status_code == 400  # used up
    assert c.get("/study/api/recordings", headers={"X-Study-Token": sam["token"]}).json()["mine"]


def test_wrong_pins_lock_for_a_minute(cloud):
    c = TestClient(cloud.create_app(cloud.MemoryStore()))
    p = c.post("/study/api/profiles", json={"name": "Alex", "pin": "1234"}).json()["profile"]
    for _ in range(5):
        assert c.post("/study/api/login", json={"profile": p["id"], "pin": "0000"}).status_code == 400
    r = c.post("/study/api/login", json={"profile": p["id"], "pin": "1234"})
    assert r.status_code == 400 and "Wait a minute" in r.json()["detail"]
