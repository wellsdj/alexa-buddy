"""Instant, deterministic handling of the things people say most. No model call, so
these work even when Groq is rate-limited or offline, and they never mis-hear a time."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from .timeparse import find_clock, find_date, find_duration, find_repeat, resolve_clock, words_to_digits

WAKE_PREFIX = re.compile(
    r"^\s*((hey|hi|hay|a|okay|ok|yo|hello)[\s,]+)?(buddy|budd?ie|body|bodie|bud)\b[\s,.!?:-]*", re.I)
NUMBER_WORDS = {"zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "max": 10, "maximum": 10, "full": 10}


@dataclass
class Intent:
    name: str
    args: dict = field(default_factory=dict)


def strip_wake(text: str) -> str:
    return WAKE_PREFIX.sub("", text, count=1).strip()


def has_wake(text: str) -> bool:
    return bool(WAKE_PREFIX.match(text or ""))


def _norm(text: str) -> str:
    t = text.lower().strip()
    t = re.sub(r"[“”\"]", "", t)
    t = re.sub(r"[.!?]+$", "", t).strip()
    t = re.sub(r"^(please|can you|could you|would you|will you|can u)\s+", "", t)
    t = re.sub(r"\s+please$", "", t)
    return t


def match(text: str, now: datetime, context: dict | None = None) -> Intent | None:
    context = context or {}
    t = _norm(text)
    if not t:
        return None

    if re.fullmatch(r"(stop|cancel|never ?mind|nevermind|shut up|be quiet|quiet|silence|"
                    r"that'?s all|nothing|forget it|no thanks?|nope|go away|stop talking|enough)", t):
        return Intent("stop")
    if re.fullmatch(r"(snooze|snooze( it| the alarm)?( for \w+ minutes?)?|five more minutes|"
                    r"(give me|just)? ?(a )?few more minutes)", t):
        d = find_duration(words_to_digits(t))
        return Intent("snooze", {"minutes": (d[0] // 60) if d else None})
    if context.get("ringing") and re.search(r"\b(stop|off|i'?m up|awake|dismiss|cancel)\b", t):
        return Intent("stop")

    if re.fullmatch(r"(thanks|thank you|cheers|ta|thank you (buddy|so much|very much)|nice one|thanks (mate|buddy))", t):
        return Intent("thanks")
    if re.fullmatch(r"(say that again|repeat( that)?|what did you (just )?say|pardon|sorry|come again|"
                    r"can you repeat that|what was that)", t):
        return Intent("repeat")
    if re.fullmatch(r"(more|keep going|carry on|next|go on|continue|and\??)", t) and context.get("pager"):
        return Intent("more")

    vol = _volume(t)
    if vol:
        return vol

    if re.search(r"\bwhat('?s| is) the time\b|\bwhat time is it\b|\bwhat time('?s| is) it\b|^time$", t):
        return Intent("time")
    if re.search(r"\b(what('?s| is) (the|today'?s) date|what day is it|what('?s| is) today|what date is it)\b", t):
        return Intent("date")

    if re.search(r"\b(what can you do|help|what are your commands|how do i use you)\b", t) and len(t) < 40:
        return Intent("help")

    if re.search(r"\b(what('?s| is) in your memory|what do you (know|remember) about me|"
                 r"what have you (learn(ed|t)|remembered)|list (your )?memor(y|ies)|read (me )?your memory)\b", t):
        return Intent("memory_list")

    alarm_list = re.search(r"\b(what|which|any|list|show|tell me)\b.*\b(alarms?|timers?|reminders?)\b", t)
    if alarm_list and not re.search(r"\b(set|cancel|delete|remove|turn off|stop)\b", t):
        kind = "timer" if "timer" in t else "reminder" if "reminder" in t else "alarm"
        return Intent("alarm_list", {"kind": kind})
    if re.search(r"\bhow (long|much time)\b.*\b(left|remaining)\b|\btime left\b", t) and "timer" in t:
        return Intent("alarm_list", {"kind": "timer"})
    m = re.fullmatch(r"(cancel|delete|remove|clear|turn off) (all )?(of )?(my |the )?(alarms|timers|reminders)", t)
    if m:
        return Intent("alarm_cancel_all", {"kind": m.group(5)[:-1]})

    w = _weather(t)
    if w:
        return w

    for fn in (_timer, _reminder, _alarm):
        hit = fn(t, now)
        if hit:
            return hit
    return None


def _volume(t: str) -> Intent | None:
    if re.fullmatch(r"(turn (it|the volume) up|volume up|louder|speak up|increase( the)? volume|a bit louder)", t):
        return Intent("volume", {"delta": 2})
    if re.fullmatch(r"(turn (it|the volume) down|volume down|quieter|softer|lower( the)? volume|decrease( the)? volume|a bit quieter)", t):
        return Intent("volume", {"delta": -2})
    if re.fullmatch(r"(mute|mute (yourself|the volume))", t):
        return Intent("volume", {"level": 0})
    m = re.fullmatch(r"(set )?(the )?volume (to |at )?(level )?(\w+)( out of 10)?", t)
    if m:
        v = m.group(5)
        level = int(v) if v.isdigit() else NUMBER_WORDS.get(v)
        if level is not None:
            return Intent("volume", {"level": max(0, min(10, level))})
    return None


def _weather(t: str) -> Intent | None:
    weathery = re.search(r"\b(weather|forecast|rain|raining|umbrella|temperature|how (hot|cold|warm)|"
                         r"sunny|snow|cold out|warm out|coat|jacket)\b", t)
    if not weathery:
        return None
    # Somewhere other than home? Let the model (with web search) handle it.
    if re.search(r"\b(in|at|for) (?!the\b|my\b|richmond\b|here\b|today\b|tomorrow\b|tonight\b|"
                 r"the morning|the afternoon|the evening|\d)[a-z]{3,}", t):
        return None
    if re.search(r"\b(week|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", t):
        return None
    day = "tomorrow" if "tomorrow" in t else "today"
    return Intent("weather", {"day": day})


def _timer(t: str, now: datetime) -> Intent | None:
    if not re.search(r"\b(timer|countdown|count down)\b", t):
        return None
    if re.search(r"\b(cancel|stop|delete|remove|how long|left)\b", t):
        return None
    d = find_duration(words_to_digits(t))
    if not d:
        return None
    label = ""
    m = re.search(r"\b(?:a|an|the)\s+([a-z]+(?:\s[a-z]+)?)\s+timer\b", t)
    if m and not re.search(r"\d|minute|second|hour|min|sec", m.group(1)):
        label = m.group(1)
    return Intent("timer", {"seconds": d[0], "label": label})


def _reminder(t: str, now: datetime) -> Intent | None:
    if not re.search(r"\bremind me\b", t):
        return None
    digits = words_to_digits(t)
    what = ""
    m = re.search(r"\bremind me (?:to|about|that)\s+(.+?)(?=\s+(?:at|in|on|tomorrow|tonight|every|by)\b|$)", digits)
    if m:
        what = m.group(1)
    else:
        m = re.search(r"\b(?:to|about|that)\s+(.+)$", digits)
        if m:
            what = m.group(1)
    what = re.sub(r"\s+(at|in|on)\s*$", "", what).strip()
    if not what:
        return None
    what = re.sub(r"\bmy\b", "your", what)
    rest = digits.replace(what, " ")
    if re.search(r"\bin \d", rest):
        d = find_duration(rest)
        if d:
            return Intent("reminder", {"seconds": d[0], "text": what})
    clock = find_clock(rest)
    if clock:
        repeat = find_repeat(rest)
        date = None if repeat else find_date(rest, now)
        if clock.meridiem is None and (date or repeat) and 1 <= clock.hour <= 7:
            clock.hour += 12  # "call mum tomorrow at 6" means the evening
            clock.meridiem = "pm"
        return Intent("reminder", {
            "time": resolve_clock(clock, now), "text": what, "repeat": repeat or [], "date": date})
    return None


def _alarm(t: str, now: datetime) -> Intent | None:
    wake = re.search(r"\bwake me( up)?\b", t)
    if not (re.search(r"\balarm\b", t) or wake):
        return None
    if re.search(r"\b(cancel|delete|remove|turn off|stop|disable|change|move|what|which|list|any)\b", t):
        return None
    digits = words_to_digits(t)
    if re.search(r"\bin \d", digits):
        d = find_duration(digits)
        if d:
            return Intent("alarm_in", {"seconds": d[0]})
    label = ""
    m = re.search(r"\b(?:called|named|labell?ed|label it)\s+(.+)$", digits)
    if m:
        label = m.group(1).strip()
        digits = digits[:m.start()]
    clock = find_clock(digits)
    if not clock:
        return Intent("alarm_needs_time", {})
    repeat = find_repeat(digits)
    date = None if repeat else find_date(digits, now)
    # With a named day (or a repeat), "next occurrence" means nothing; alarms at 5-11 are mornings.
    morning = bool(wake) or "morning" in digits or bool(date or repeat)
    hhmm = resolve_clock(clock, now, prefer_morning=morning)
    return Intent("alarm", {"time": hhmm, "repeat": repeat or [], "label": label, "date": date})
