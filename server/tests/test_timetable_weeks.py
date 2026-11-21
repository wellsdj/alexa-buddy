"""Week A / Week B timetable: the right week, the right day, said nicely."""
from datetime import date, datetime

from buddy.timetable import Timetable, which_day


def test_weeks_alternate_from_what_you_said(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_week("A", {"Monday": [{"start": "08:50", "subject": "Maths", "room": "M4"}]})
    tt.set_week("B", {"Monday": [{"start": "08:50", "subject": "Art", "teacher": "Ms Lee"}]})
    tt.set_this_week("B", date(2026, 11, 18))  # a Wednesday in week B
    assert tt.week_of(date(2026, 11, 16)) == "B" and tt.week_of(date(2026, 11, 23)) == "A"
    assert tt.spoken(date(2026, 11, 23)) == "Monday, week A: form, then period 1 Maths."
    assert "period 1 Art" in tt.spoken(date(2026, 11, 16)) and "Lee" not in tt.spoken(date(2026, 11, 16))
    assert "weekend" in tt.spoken(date(2026, 11, 21))


def test_asks_which_week_if_it_doesnt_know(tmp_path):
    tt = Timetable(tmp_path)
    assert "week A or week B" in tt.spoken(date(2026, 11, 16))


def test_the_built_in_timetable(tmp_path):
    tt = Timetable(tmp_path)
    tt.set_this_week("A", date(2026, 11, 16))
    assert tt.spoken(date(2026, 11, 16)) == ("Monday, week A: form, then period 1 RE, period 2 Music, "
                                             "period 3 Textiles, period 4 Computer Studies, period 5 Science.")
    assert "period 1 Design Technology" in tt.spoken(date(2026, 11, 24))  # Tuesday, week B


def test_days():
    now = datetime(2026, 11, 18, 9)  # Wednesday
    assert which_day("tomorrow", now) == date(2026, 11, 19)
    assert which_day("monday", now) == date(2026, 11, 23)
    assert which_day("what's on today", now) == date(2026, 11, 18)
