from datetime import datetime
from zoneinfo import ZoneInfo

from buddy.alarms import AlarmStore

LONDON = ZoneInfo("Europe/London")


def ts(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=LONDON).timestamp()


def test_one_shot_rolls_to_tomorrow(tmp_path):
    store = AlarmStore(tmp_path / "a.json")
    now = ts(2026, 9, 30, 22, 15)
    a = store.add_alarm("07:00", now=now)
    assert a.at == ts(2026, 10, 1, 7, 0)
    assert store.confirm_text(a, now) == "Alarm set for 7 AM tomorrow, Thursday. That's 8 hours and 45 minutes from now."


def test_weekday_alarm_skips_weekend(tmp_path):
    store = AlarmStore(tmp_path / "a.json")
    fri_night = ts(2026, 10, 2, 21, 0)
    a = store.add_alarm("06:30", "weekdays", now=fri_night)
    assert store.next_fire(a, fri_night) == ts(2026, 10, 5, 6, 30)  # Monday
    assert store.confirm_text(a, fri_night) == "Alarm set for 6:30 AM every weekday. The first one is Monday."
    wed = ts(2026, 9, 30, 16, 40)
    assert store.confirm_text(a, wed).endswith("The first one is tomorrow.")


def test_repeating_alarm_survives_clock_change(tmp_path):
    # UK clocks go back on Sunday 25 October 2026: 07:00 must stay 07:00 local.
    store = AlarmStore(tmp_path / "a.json")
    fri = ts(2026, 10, 23, 12, 0)
    store.add_alarm("07:00", "daily", now=fri)
    fires = [t for t, x in store.upcoming(fri, horizon_s=3 * 86400)]
    assert [datetime.fromtimestamp(t, LONDON).hour for t in fires] == [7, 7, 7]
    gaps = [fires[i + 1] - fires[i] for i in range(len(fires) - 1)]
    assert gaps == [25 * 3600, 24 * 3600]  # Sat->Sun spans the extra hour


def test_device_schedule_and_one_shot_removal(tmp_path):
    store = AlarmStore(tmp_path / "a.json")
    now = ts(2026, 9, 30, 10, 0)
    t = store.add_timer(300, "pasta", now=now)
    r = store.add_alarm("09:00", "daily", "take your pills", kind="reminder", now=now)
    sched = store.device_schedule(now)
    assert sched[0] == {"id": t.id, "at": int(now + 300), "kind": "timer"}
    assert sum(1 for s in sched if s["id"] == r.id) == 8  # the device gets 8 days ahead
    store.fired(t.id, now + 300)
    assert store.get(t.id) is None
    assert store.last_fired(now=now + 400).label == "pasta"
    store.fired(r.id, now)
    assert store.get(r.id) is not None  # repeating alarms stay


def test_persistence(tmp_path):
    p = tmp_path / "a.json"
    a = AlarmStore(p).add_alarm("08:15", "weekends", "lie in")
    again = AlarmStore(p)
    assert again.get(a.id).label == "lie in" and again.get(a.id).days == [5, 6]


def test_describe(tmp_path):
    store = AlarmStore(tmp_path / "a.json")
    now = ts(2026, 9, 30, 10, 0)
    a = store.add_alarm("07:30", "mon,wed", now=now)
    assert store.describe(a, now) == "alarm at 7:30 AM on Mondays and Wednesdays"
    t = store.add_timer(125, now=now)
    assert store.describe(t, now) == "a timer with 2 minutes and 5 seconds left"
