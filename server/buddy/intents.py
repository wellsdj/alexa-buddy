"""Instant, deterministic handling of the things people say most. No model call, so
these work even when the internet is down, and they never mis-hear a time."""
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


# "Off" and friends: Buddy goes quiet and stops listening to the room until the next "hey buddy".
_OFF_CORE = (r"(turn|switch|power|shut) (yourself |it )?(off|down)|(you can )?(turn|switch) off|off|"
             r"power down|go (to sleep|away|quiet)|sleep|shut up|be quiet|quiet|silence|hush|shush|"
             r"stop (listening|talking|it)|leave me alone|that'?s (all|it|enough)|enough|i'?m (done|good|fine)|"
             r"we'?re done|(good)?bye|see you( later)?|later|you can go|off you go|that'?ll do|done")
_OFF = re.compile(
    r"((ok|okay|alright|all right|right|yeah|yes|cool|great|fine|thanks|thank you|cheers|buddy|please|"
    r"now|just|you can|can you|could you|and|so|well|oh|um|uh)[ ,]*)*"
    r"(" + _OFF_CORE + r")"
    r"([ ,]*(now|then|please|thanks|thank you|cheers|buddy|for now|mate|for a bit|for a while|bye|"
    r"goodbye|off|already))*")


def is_off(text: str) -> bool:
    """Short "go away" type sentences, in lots of wordings. Never things like "turn off my alarm"."""
    t = _norm(text) if text else ""
    t = re.sub(r"[,.!?]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    if not t or len(t.split()) > 8 or re.search(r"\b(alarm|timer|reminder|light|tv|music|radio|heating)s?\b", t):
        return False
    return bool(_OFF.fullmatch(t))


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

    if context.get("ringing") and re.search(r"\b(stop|off|i'?m up|awake|dismiss|cancel)\b", t):
        return Intent("stop")
    if re.fullmatch(r"(snooze|snooze( it| the alarm)?( for \w+ minutes?)?|five more minutes|"
                    r"(give me|just)? ?(a )?few more minutes)", t):
        d = find_duration(words_to_digits(t))
        return Intent("snooze", {"minutes": (d[0] // 60) if d else None})
    if is_off(t):
        return Intent("off")
    if re.fullmatch(r"(stop|cancel|never ?mind|nevermind|nothing|forget it|no thanks?|nope|no)", t):
        return Intent("stop")

    voice = _voice(t)
    if voice:
        return voice
    music = _music(t)
    if music:
        return music

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


def _music(t: str) -> Intent | None:
    """Spotify: the quick ones. Anything fancier ("play something chill for studying") goes to Claude."""
    t = re.sub(r"^(buddy|ok|okay|hey)[ ,]+", "", t)
    if re.fullmatch(r"(pause|stop|turn off|kill|mute)( the)? (music|song|spotify|tune)s?|pause( it| that)?|music off", t):
        return Intent("music", {"action": "pause"})
    if re.fullmatch(r"(resume|unpause|carry on|continue|play|start|put on|turn on)( the)? (music|song|spotify|tunes)"
                    r"( again)?|resume( it)?|unpause( it)?|music on", t):
        return Intent("music", {"action": "resume"})
    if re.fullmatch(r"(skip|next)( (this|it|that))?( (song|track|tune|one))?( please)?|next( song| track| tune)?"
                    r"( please)?|skip (this|the) (song|track|tune)", t):
        return Intent("music", {"action": "next"})
    if re.fullmatch(r"(previous|last|go back( to the last)?)( song| track| tune)( please)?|play the (last|previous) "
                    r"(song|track)( again)?|back a (song|track)", t):
        return Intent("music", {"action": "previous"})
    if re.fullmatch(r"what('?s| is) (this|that|playing)( song| track| tune)?|what song is (this|that|playing)|"
                    r"who (sings|is singing|is) this( song)?|what('?s| is) this song called", t):
        return Intent("music", {"action": "now_playing"})
    m = re.fullmatch(r"(play|put on|stick on|chuck on)( me)? (.+?)( on spotify)?( please)?", t)
    if m and m.group(3) not in ("it", "that", "the music", "a game"):
        return Intent("music", {"action": "play", "query": m.group(3)})
    return None


def _voice(t: str) -> Intent | None:
    """Switching between the voices saved in Controls > Voices."""
    t = re.sub(r"^(buddy|ok|okay|hey)[ ,]+", "", t)
    if re.fullmatch(r"(go back to |use |switch to |change to )?(your |the )?(previous|last|old|other) voice( again)?|"
                    r"(go )?back to (your |the )?(previous|last|old|other) voice", t):
        return Intent("voice", {"step": -1})
    m = re.fullmatch(r"(use|switch to|change to|go to|try) voice (number )?(\w+)", t)
    if m:
        n = words_to_digits(m.group(3))
        if n.isdigit():
            return Intent("voice", {"number": int(n)})
    if re.fullmatch(r"((can you |could you |please )?(change|switch|swap|cycle|rotate|try) (your |the |a |to (a |the )?)?"
                    r"(different |new |next |another )?voices?( please| again| now)?|(next|another|different|new) voice( please)?|"
                    r"(use|try) (a |another |the next )?(different |other |new )voice( please)?|change it up)", t):
        return Intent("voice", {"step": 1})
    m = re.fullmatch(r"(use|switch to|change to|go to|try) (the )?(.+?)(?:'s)? voice", t)
    if m and m.group(3) not in ("a", "your", "my", "different", "another", "new", "next", "other"):
        return Intent("voice", {"name": m.group(3)})
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
    # Any place ("in Paris", "for Manchester") and any day up to 10 days ahead.
    m = re.search(r"\b(?:in|for|at|over) (?!the (?:morning|afternoon|evening|week|weekend)\b|the next\b|my\b|here\b|"
                  r"today\b|tomorrow\b|tonight\b|this\b|next\b|a\b|\d)([a-z][a-z'. -]*?)"
                  r"(?= (?:today|tomorrow|tonight|this|next|on|over|for|at|in \d|in (?:a|one|two|three|four|five|six|"
                  r"seven|eight|nine|ten) days?|the weekend|the week)\b|$)", t)
    place = m.group(1).strip() if m else ""
    if place in ("the", "it", "there", "general", "a bit", "a while") or len(place) < 3:
        place = ""
    return Intent("weather", {"place": place, "when": t})


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
