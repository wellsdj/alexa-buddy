"""5am homework: read the Bromcom page straight off, one Claude call to sum it up; skip Saturdays and holidays."""
import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from buddy.hub import homework_day_skip
from buddy.main import create_app
from buddy.school_calendar import SchoolCalendar
from tests.fakes import claude_message, claude_text

PAGE = """Homework\nSubject  Title  Set  Due  Status\nMaths  Simultaneous equations worksheet  Mon 23 Nov  Tue 1 Dec
Not submitted\nFood Tech  Bring ingredients for crumble  Wed 25 Nov  Mon 30 Nov  Not submitted"""
NOW = datetime(2026, 11, 27, 5, 1, tzinfo=ZoneInfo("Europe/London"))


def hub_with(settings, apis, claude, page):
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    hub = app.state.hub
    opened = []

    async def read_page(url, which="school", wait_s=4.0):
        opened.append((url, which))
        return page
    hub.online.read_page = read_page
    return app, hub, opened


def test_reads_the_page_and_makes_one_claude_call(settings, apis, claude):
    claude.script = [claude_message(claude_text("Food Tech, due Monday: bring crumble ingredients. "
                                                "Maths, due Tuesday: the simultaneous equations worksheet."))]
    app, hub, opened = hub_with(settings, apis, claude, PAGE)
    with TestClient(app):
        assert asyncio.run(hub.read_homework(NOW))
    assert opened == [("https://www.bromcomvle.com/Homework", "school")]
    assert len(claude.requests) == 1 and "Simultaneous equations" in str(claude.requests[0]["messages"])
    assert "Saturday 28 November" in str(claude.requests[0]["system"])  # "due tomorrow or later"
    assert hub.brain.homework_ready[1].startswith("Food Tech, due Monday")
    assert any("Homework from Bromcom" in m.text for m in hub.memory.list())


def test_a_changed_page_falls_back_to_the_agent(settings, apis, claude):
    claude.script = [claude_message(claude_text("NOT_HOMEWORK_PAGE"))]
    app, hub, _ = hub_with(settings, apis, claude, "Sign in to continue with Google")
    with TestClient(app):
        assert not asyncio.run(hub.read_homework(NOW))
    assert hub.brain.homework_ready[1] == ""


def test_no_page_at_all_falls_back_without_asking_claude(settings, apis, claude):
    app, hub, _ = hub_with(settings, apis, claude, None)
    with TestClient(app):
        assert not asyncio.run(hub.read_homework(NOW))
    assert not claude.requests


def test_saturdays_and_holidays_are_skipped_but_sunday_runs(tmp_path):
    cal = SchoolCalendar(tmp_path)
    assert homework_day_skip(date(2026, 11, 27), cal) == ""  # Friday
    assert homework_day_skip(date(2026, 11, 28), cal) == "Saturday"
    assert homework_day_skip(date(2026, 11, 29), cal) == ""  # Sunday: homework for Monday
    assert homework_day_skip(date(2026, 12, 22), cal) == "the Christmas holidays"
    assert homework_day_skip(date(2026, 12, 27), cal) == "the Christmas holidays"  # Sunday, Monday's a holiday
    assert homework_day_skip(date(2027, 1, 3), cal) == ""  # Sunday before term starts
