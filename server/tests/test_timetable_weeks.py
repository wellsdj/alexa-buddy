"""Week A / Week B timetable: the right week, the right day, said nicely."""
from datetime import date, datetime

from buddy.timetable import Timetable, which_day


def test_weeks_alternate_from_what_you_said(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_week("A", {"Monday": [{"start": "08:50", "subject": "Maths", "room": "M4"}]})
    tt.set_week("B", {"Monday": [{"start": "08:50", "subject": "Art", "teacher": "Ms Lee"}]})
    tt.set_this_week("B", date(2026, 11, 18))  # a Wednesday in week B
    assert tt.week_of(date(2026, 11, 16)) == "B" and tt.week_of(date(2026, 11, 23)) == "A"
    assert tt.spoken(date(2026, 11, 23)) == "Monday, week A. Form first. Period 1, Maths."
    assert "Period 1, Art." in tt.spoken(date(2026, 11, 16)) and "Lee" not in tt.spoken(date(2026, 11, 16))
    assert "weekend" in tt.spoken(date(2026, 11, 21))


def test_the_built_in_timetable(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_this_week("A", date(2026, 11, 16))
    assert tt.spoken(date(2026, 11, 16)) == ("Monday, week A. Form first. Period 1, RE. Period 2, Music. "
                                             "Period 3, Textiles. Period 4, Computer Studies. Period 5, Science.")
    assert "Period 1, Design Technology." in tt.spoken(date(2026, 11, 24))  # Tuesday, week B


def test_said_on_a_sunday_this_week_means_the_coming_one(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_this_week("B", date(2026, 11, 22))  # Sunday
    assert tt.week_of(date(2026, 11, 23)) == "B"


def test_holidays_and_the_last_day_of_term(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_this_week("A", date(2026, 11, 16))
    assert "Christmas holidays, so no school" in tt.spoken(date(2026, 12, 22))
    assert tt.spoken(date(2026, 12, 18)).endswith("last day of term.")


def test_days():
    now = datetime(2026, 11, 18, 9)  # Wednesday
    assert which_day("tomorrow", now) == date(2026, 11, 19)
    assert which_day("monday", now) == date(2026, 11, 23)
    assert which_day("what's on today", now) == date(2026, 11, 18)


def test_asking_about_the_day_never_goes_online(settings, apis, claude):
    import asyncio
    from buddy.brain import Conversation
    from tests.test_llm_brain import make_brain
    brain = make_brain(settings, apis, claude)
    brain.now = lambda: datetime(2026, 11, 18, 9)  # a Wednesday
    conv = Conversation()
    r = asyncio.run(brain.handle("what do I have today", conv))
    assert r.text.startswith("Wednesday, week A. Form first. Period 1, Maths.") and not claude.requests
    r = asyncio.run(brain.handle("what have I got tomorrow", conv))
    assert not claude.requests
    r = asyncio.run(brain.handle("what's my homework", conv))
    assert r.intent != "timetable"


def test_knows_the_week_without_asking(tmp_path):
    tt = Timetable(tmp_path)
    assert tt.week_of(date(2026, 11, 23)) == "B" and tt.week_of(date(2026, 11, 30)) == "A"
    assert "week B" in tt.spoken(date(2026, 11, 23))


def test_do_i_have_pe_and_say_it_slowly(settings, apis, claude):
    import asyncio
    from buddy.brain import Conversation
    from tests.test_llm_brain import make_brain
    brain = make_brain(settings, apis, claude)
    brain.now = lambda: datetime(2026, 11, 22, 10)  # Sunday; tomorrow is Monday of week B
    conv = Conversation()
    r = asyncio.run(brain.handle("what have I got tomorrow", conv))
    assert r.text.startswith("Monday, week B. Form first. Period 1, English.")
    r2 = asyncio.run(brain.handle("can you tell me that really slowly", conv))
    assert r2.text == r.text
    r = asyncio.run(brain.handle("do I have PE tomorrow", conv))
    assert r.text == "No, no PE tomorrow."
    r = asyncio.run(brain.handle("do I have drama tomorrow", conv))
    assert r.text == "Yes, you've got Drama tomorrow, period 2."
    assert not claude.requests  # none of it needed Claude, let alone the web


def test_an_old_saved_week_is_replaced(tmp_path):
    import json
    (tmp_path / "timetable.json").write_text(json.dumps({"weeks": {"A": {"Monday": ["RE"]}, "B": {"Monday": ["English"]}},
                                                         "week_a_monday": "2026-11-09"}))
    assert Timetable(tmp_path).week_of(date(2026, 11, 23)) == "B"


def test_other_ways_of_asking_about_a_subject(settings, apis, claude):
    import asyncio
    from buddy.brain import Conversation
    from tests.test_llm_brain import make_brain
    brain = make_brain(settings, apis, claude)
    brain.now = lambda: datetime(2026, 11, 22, 10)  # Sunday
    for q in ("is there PE tomorrow", "um have I got P.E. tomorrow?", "any maths on Wednesday"):
        r = asyncio.run(brain.handle(q, Conversation()))
        assert r.intent == "timetable", q
    assert "Maths" in asyncio.run(brain.handle("any maths on Wednesday", Conversation())).text
    assert not claude.requests


def test_the_schools_list_of_week_a_mondays_beats_counting(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_this_week("A", date(2026, 11, 16))  # counting alone: 23 Nov would be B, 4 Jan A ...
    # ... but say the school restarts at Week A after Christmas: its list wins inside the dates it covers.
    assert tt.set_week_a_mondays(["2026-11-16", "2026-11-30", "2027-01-04", "2027-01-18", "not a date",
                                  "2027-01-20"]) == 4  # 20 Jan isn't a Monday
    assert tt.week_of(date(2026, 11, 25)) == "B"
    assert tt.week_of(date(2027, 1, 5)) == "A" and tt.week_of(date(2027, 1, 13)) == "B"
    assert tt.week_of(date(2027, 1, 21)) == "A"


def test_once_a_term(tmp_path):
    from buddy.school_calendar import SchoolCalendar
    cal = SchoolCalendar(tmp_path)
    assert cal.term_started_since(None, date(2026, 11, 27))
    assert not cal.term_started_since(date(2026, 11, 2), date(2026, 11, 27))  # same half term
    assert cal.term_started_since(date(2026, 12, 18), date(2027, 1, 4))  # back after Christmas


def test_the_week_check_uses_the_week_it_ran_in_not_a_date_off_the_page(settings, apis):
    import time as _time
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from buddy.main import create_app
    app = create_app(settings, apis.client(), None, apis.stt())
    with TestClient(app):
        ran = _time.mktime((2026, 11, 25, 5, 30, 0, 0, 0, -1))  # Wednesday 25 Nov, week B
        job = SimpleNamespace(kind="school_week", status="done", started=ran, signin_failed=False,
                              result='{"week": "B", "monday": "2026-11-30"}')  # a wrong date on the page
        app.state.hub.morning_job_done(job)
    tt = Timetable(settings.data_dir)
    assert tt.week_of(date(2026, 11, 23)) == "B" and tt.week_of(date(2026, 11, 30)) == "A"
