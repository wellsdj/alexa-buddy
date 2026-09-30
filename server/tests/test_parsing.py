from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from buddy.intents import match, strip_wake
from buddy.timeparse import find_duration, words_to_digits

LONDON = ZoneInfo("Europe/London")
EVENING = datetime(2026, 9, 30, 22, 15, tzinfo=LONDON)  # Wednesday 22:15
AFTERNOON = datetime(2026, 9, 30, 15, 0, tzinfo=LONDON)


@pytest.mark.parametrize("text,time,repeat,date", [
    ("Hey buddy, set an alarm for seven", "07:00", [], None),
    ("wake me up at half seven", "07:30", [], None),
    ("set an alarm for 7:30 pm", "19:30", [], None),
    ("alarm for quarter to eight tomorrow", "07:45", [], "2026-10-01"),
    ("set an alarm for 6 30 every weekday", "06:30", [0, 1, 2, 3, 4], None),
    ("wake me up at 8 on saturday", "08:00", [], "2026-10-03"),
    ("set an alarm for seven thirty every monday and wednesday", "07:30", [0, 2], None),
    ("set an alarm for 19:45", "19:45", [], None),
    ("alarm at noon tomorrow", "12:00", [], "2026-10-01"),
    ("set an alarm for 11", "23:00", [], None),  # the next 11 o'clock after 22:15
    ("set alarm for twenty past six every day", "06:20", [0, 1, 2, 3, 4, 5, 6], None),
])
def test_alarm_phrases(text, time, repeat, date):
    it = match(strip_wake(text), EVENING)
    assert it and it.name == "alarm"
    assert it.args["time"] == time
    assert it.args["repeat"] == repeat
    assert it.args["date"] == date


def test_ambiguous_hour_picks_next_occurrence():
    assert match("set an alarm for 7", AFTERNOON).args["time"] == "19:00"
    assert match("wake me up at 7", AFTERNOON).args["time"] == "07:00"  # waking up means morning


@pytest.mark.parametrize("text,seconds,label", [
    ("set a timer for 10 minutes", 600, ""),
    ("set a pasta timer for twelve minutes", 720, "pasta"),
    ("timer for an hour and a half", 5400, ""),
    ("set a 5 minute timer", 300, ""),
    ("set a timer for 1 hour 20", 4800, ""),
    ("set a timer for 90 seconds", 90, ""),
    ("set a timer for half an hour", 1800, ""),
])
def test_timers(text, seconds, label):
    it = match(text, EVENING)
    assert it.name == "timer" and it.args == {"seconds": seconds, "label": label}


def test_reminders():
    r = match("remind me to take the bins out at 8 pm", EVENING)
    assert r.name == "reminder" and r.args["time"] == "20:00" and r.args["text"] == "take the bins out"
    r = match("remind me in 20 minutes to check the oven", EVENING)
    assert r.args == {"seconds": 1200, "text": "check the oven"}
    r = match("remind me to call mum tomorrow at 6", EVENING)
    assert r.args["time"] == "18:00" and r.args["date"] == "2026-10-01"
    r = match("remind me to take my pills every day at 9 am", EVENING)
    assert r.args["text"] == "take your pills" and r.args["repeat"] == [0, 1, 2, 3, 4, 5, 6]


@pytest.mark.parametrize("text,name,args", [
    ("what's the weather", "weather", {"day": "today"}),
    ("will it rain tomorrow", "weather", {"day": "tomorrow"}),
    ("do I need an umbrella", "weather", {"day": "today"}),
    ("what time is it", "time", {}),
    ("volume up", "volume", {"delta": 2}),
    ("set the volume to 7", "volume", {"level": 7}),
    ("stop", "stop", {}),
    ("thanks buddy", "thanks", {}),
    ("what alarms do I have", "alarm_list", {"kind": "alarm"}),
    ("cancel all my alarms", "alarm_cancel_all", {"kind": "alarm"}),
    ("what's in your memory", "memory_list", {}),
    ("what do you know about me", "memory_list", {}),
    ("set an alarm", "alarm_needs_time", {}),
    ("wake me up in 20 minutes", "alarm_in", {"seconds": 1200}),
])
def test_fast_paths(text, name, args):
    it = match(text, EVENING)
    assert it.name == name and it.args == args


@pytest.mark.parametrize("text", [
    "what's the weather in Paris", "what is the weather like this weekend",
    "tell me a joke", "who won the football last night", "what's the capital of Peru",
])
def test_goes_to_the_model(text):
    assert match(text, EVENING) is None


def test_strip_wake_variants():
    assert strip_wake("Hey Buddy, what's up?") == "what's up?"
    assert strip_wake("hey buddie set a timer") == "set a timer"
    assert strip_wake("Buddy. Stop.") == "Stop."
    assert strip_wake("what's the weather") == "what's the weather"


def test_number_words():
    assert words_to_digits("seven thirty five") == "7 35"
    assert words_to_digits("twenty one minutes") == "21 minutes"
    assert find_duration("2 hours and 5 minutes")[0] == 7500
