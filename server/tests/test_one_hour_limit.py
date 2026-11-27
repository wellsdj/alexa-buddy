"""Nothing runs longer than an hour: a spoken request, a lesson being made, or a job online."""
import asyncio
import json

from fastapi.testclient import TestClient

import buddy.hub as hub_mod
import buddy.study as study_mod
from buddy import online
from buddy.config import MAX_REQUEST_SECONDS
from buddy.main import create_app
from buddy.study import StudyStore, StudyWorker
from tests.test_server import recv_until


def test_the_limit_is_an_hour_and_online_jobs_sit_inside_it():
    assert MAX_REQUEST_SECONDS == 3600
    assert online.MAX_SECONDS <= MAX_REQUEST_SECONDS


def test_a_request_over_the_limit_is_stopped(settings, apis, monkeypatch):
    monkeypatch.setattr(hub_mod, "MAX_REQUEST_SECONDS", 0.5)  # an hour, sped up
    app = create_app(settings, apis.client(), None, apis.stt())
    cancelled = []

    async def forever(text, conv, ctx=None):
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.append(text)
            raise
    app.state.hub.brain.handle = forever
    with TestClient(app) as client, client.websocket_connect("/ws?role=ui&name=test") as ws:
        recv_until(ws, lambda m: m.get("type") == "hello_ack")
        ws.send_text(json.dumps({"type": "text", "text": "do something that never ends"}))
        start, _ = recv_until(ws, lambda m: m.get("type") == "audio_start" and "hour" in m.get("text", ""))
        assert "stopped it completely" in start["text"]
    assert cancelled == ["do something that never ends"]  # killed, not left running


def test_a_lesson_over_the_limit_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(study_mod, "MAX_REQUEST_SECONDS", 0.2)
    s = StudyStore(tmp_path)
    a = s.add_profile("Alex", "1234")
    lesson = s.new_lesson(a["id"], "Maths", "", "", "text")
    w = StudyWorker(s, None, None)

    async def forever(pid, lid):
        await asyncio.sleep(3600)
    w.process = forever

    async def go():
        task = asyncio.create_task(w._run())
        w.queue.put_nowait((a["id"], lesson["id"]))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if s.lesson(a["id"], lesson["id"])["status"] == "failed":
                break
        task.cancel()
    asyncio.run(go())
    done = s.lesson(a["id"], lesson["id"])
    assert done["status"] == "failed" and "over an hour" in done["error"]
