"""Cancelling a job, homework ready from 5am, weather ready, and term dates."""
import asyncio
import time
from datetime import date

from buddy.brain import Conversation
from buddy.school_calendar import SchoolCalendar
from tests.fakes import claude_message, claude_tool
from tests.test_llm_brain import make_brain


class FakeOnline:
    busy = True
    waiting = False
    stopped = False

    def stop(self):
        self.stopped = True
        return "stopped"


def test_cancel_a_job(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    brain.online = FakeOnline()
    r = asyncio.run(brain.handle("cancel the homework check", Conversation()))
    assert brain.online.stopped and r.intent == "online" and not claude.requests


def test_homework_from_this_morning_needs_no_going_online(settings, apis, claude):
    from tests.fakes import claude_text
    brain = make_brain(settings, apis, claude)
    started = []
    brain.online = type("O", (), {"busy": False, "waiting": False, "start": lambda *a, **k: started.append(1)})()
    brain.homework_ready = (time.time(), "Maths worksheet due Tuesday.")
    claude.script = [claude_message(claude_tool("school", {"what": "homework"})),
                     claude_message(claude_text("You've got a Maths worksheet due Tuesday."))]
    r = asyncio.run(brain.handle("have I got any homework", Conversation()))
    assert not started and "Maths worksheet" in claude.requests[1]["messages"][-1]["content"][0]["content"]


def test_weather_ready_is_quick(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    brain.weather_ready = (time.time(), "Sunny and 12 degrees.")
    t0 = time.monotonic()
    r = asyncio.run(brain.handle("what's the weather", Conversation()))
    took = time.monotonic() - t0
    assert r.text.startswith("Sunny") or "Sunny" in r.text
    assert 0.3 < took < 1.0


def test_term_dates(tmp_path):
    cal = SchoolCalendar(tmp_path)
    assert cal.why_off(date(2026, 10, 28)) == "October half term"
    assert cal.last_day_of_term(date(2026, 12, 18)) and not cal.last_day_of_term(date(2026, 12, 11))
    assert "back on Monday 04 January" in cal.note(date(2026, 12, 23))
    cal.save([{"from": "2026-11-27", "to": "2026-11-27", "what": "an INSET day"}])
    assert cal.why_off(date(2026, 11, 27)) == "an INSET day"
