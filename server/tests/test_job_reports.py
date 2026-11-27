"""A job online that went wrong is mentioned once, in one line, and never after you cancelled it."""
import asyncio
from types import SimpleNamespace

from fastapi.testclient import TestClient

from buddy.main import create_app
from buddy.online import STOP_WORDS, Job


class Ear:
    def __init__(self):
        self.id, self.state, self.turn_running, self.said = "s1", "idle", 0, []

    async def speak(self, text, then_listen=False):
        self.said.append((text, then_listen))


def report_all(settings, apis, jobs):
    app = create_app(settings, apis.client(), None, apis.stt())
    with TestClient(app):
        hub = app.state.hub
        ear = Ear()
        hub.devices = lambda: [ear]
        hub.last_spoke_at = 0

        async def go():
            for j in jobs:
                await hub.online_report(j)
        asyncio.run(go())
        return ear.said, hub.conv.pending


def failed(topic="the tennis booking", result="Pensford has no courts free"):
    j = Job(task="book tennis", kind="tennis", topic=topic)
    j.status, j.result, j.started = "failed", result, 0
    return j


def test_a_failure_is_said_once_with_no_question(settings, apis):
    said, pending = report_all(settings, apis, [failed(), failed(), failed(result="sign-in failed")])
    assert said == [("I couldn't manage the tennis booking: Pensford has no courts free.", False)]
    assert "?" not in said[0][0] and pending is None


def test_a_job_you_stopped_is_never_reported(settings, apis):
    j = failed()
    j.stopped_by_user = True
    s = Job(task="x")
    s.status = "stopped"
    said, pending = report_all(settings, apis, [j, s])
    assert said == [] and pending is None


def test_cancel_words_stop_the_job():
    for t in ("cancel it", "never mind", "forget it", "stop", "leave it", "nevermind"):
        assert STOP_WORDS.search(t), t
    assert not STOP_WORDS.search("6pm please")
