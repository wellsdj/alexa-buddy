"""Parsing spoken times and durations: "half seven", "quarter to eight", "7:30pm",
"an hour and a half", "90 seconds", "tomorrow", "every weekday"..."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

UNITS = {
    "zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19,
}
TENS = {"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
        "eighty": 80, "ninety": 90}
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def words_to_digits(text: str) -> str:
    """'seven thirty five' -> '7 35', 'twenty one' -> '21', 'a couple of' -> '2'."""
    text = text.lower()
    text = re.sub(r"\ba couple of\b", "2", text)
    text = re.sub(r"\ba few\b", "3", text)
    tokens = re.findall(r"[a-z']+|\d+|[^\sa-z\d]", text)
    out: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in TENS:
            val = TENS[t]
            if i + 1 < len(tokens) and tokens[i + 1] in UNITS and 0 < UNITS[tokens[i + 1]] < 10:
                val += UNITS[tokens[i + 1]]
                i += 1
            out.append(str(val))
        elif t in UNITS and t != "oh":
            out.append(str(UNITS[t]))
        elif t == "oh" and out and out[-1].isdigit() and i + 1 < len(tokens) and tokens[i + 1] in UNITS:
            out.append(f"0{UNITS[tokens[i + 1]]}")
            i += 1
        else:
            out.append(t)
        i += 1
    s = " ".join(out)
    s = re.sub(r"\s+([:.,?!])", r"\1", s)
    s = re.sub(r"(\d)\s*:\s*(\d)", r"\1:\2", s)
    return s


@dataclass
class Clock:
    hour: int
    minute: int
    meridiem: str | None  # "am" | "pm" | None
    span: tuple[int, int]


def find_clock(text: str) -> Clock | None:
    """Find the first clock time in (already digitised) text."""
    t = text.lower()
    m = re.search(r"\b(noon|midday)\b", t)
    if m:
        return Clock(12, 0, "pm", m.span())
    m = re.search(r"\bmidnight\b", t)
    if m:
        return Clock(0, 0, "am", m.span())
    mer = r"\s*(a\.?\s?m\.?|p\.?\s?m\.?|in the morning|in the afternoon|in the evening|at night|tonight|o'?clock)?"
    patterns = [
        (r"\bhalf past (\d{1,2})\b" + mer, lambda g: (int(g[0]), 30)),
        (r"\bquarter past (\d{1,2})\b" + mer, lambda g: (int(g[0]), 15)),
        (r"\bquarter to (\d{1,2})\b" + mer, lambda g: ((int(g[0]) - 1) % 24, 45)),
        (r"\b(\d{1,2}) (?:minutes? )?past (\d{1,2})\b" + mer, lambda g: (int(g[1]), int(g[0]))),
        (r"\b(\d{1,2}) (?:minutes? )?to (\d{1,2})\b" + mer, lambda g: ((int(g[1]) - 1) % 24, 60 - int(g[0]))),
        (r"\bhalf (\d{1,2})\b" + mer, lambda g: (int(g[0]), 30)),  # British "half seven" = 7:30
        (r"\b(\d{1,2})[:.](\d{2})\b" + mer, lambda g: (int(g[0]), int(g[1]))),
        (r"\b(\d{1,2}) (\d{2})\b" + mer, lambda g: (int(g[0]), int(g[1]))),
        (r"\b(\d{3,4})\s*(a\.?\s?m\.?|p\.?\s?m\.?|hours)\b", None),
        (r"\b(\d{1,2})\b" + mer, lambda g: (int(g[0]), 0)),
    ]
    for pat, fn in patterns:
        m = re.search(pat, t)
        if not m:
            continue
        g = m.groups()
        if fn is None:
            digits = g[0]
            h, mi = int(digits[:-2]), int(digits[-2:])
            suffix = g[1]
        else:
            h, mi = fn(g)
            suffix = g[-1]
        if not (0 <= h <= 23 and 0 <= mi <= 59):
            continue
        meridiem = None
        if suffix:
            s = suffix.replace(".", "").replace(" ", "")
            if s.startswith("am") or "morning" in s:
                meridiem = "am"
            elif s.startswith("pm") or any(w in s for w in ("afternoon", "evening", "night", "tonight")):
                meridiem = "pm"
        if meridiem == "pm" and h < 12:
            h += 12
        elif meridiem == "am" and h == 12:
            h = 0
        # An hour past 12 ("19:30") is already unambiguous; don't let resolve_clock flip it.
        return Clock(h, mi, meridiem or ("24h" if h > 12 else None), m.span())
    return None


def resolve_clock(c: Clock, now: datetime, prefer_morning: bool = False) -> str:
    """Pick the right 24h time. '7' with no am/pm means whichever 7 comes next,
    unless it's a wake-up alarm, where a morning hour is the obvious reading."""
    h = c.hour
    if c.meridiem is None and 1 <= h <= 12:
        if prefer_morning and 4 <= h <= 11:
            return f"{h:02d}:{c.minute:02d}"
        options = [h % 12, h % 12 + 12]
        best = None
        for hh in options:
            cand = now.replace(hour=hh, minute=c.minute, second=0, microsecond=0)
            if cand <= now:
                cand += timedelta(days=1)
            if best is None or cand < best[0]:
                best = (cand, hh)
        h = best[1]
    return f"{h:02d}:{c.minute:02d}"


def find_duration(text: str) -> tuple[int, tuple[int, int]] | None:
    """Seconds and span of the first duration: '10 minutes', 'an hour and a half', '1 hour 20'."""
    t = text.lower()
    t = t.replace("half an hour", "30 minutes").replace("half a minute", "30 seconds")
    t = re.sub(r"\ban?\s+(hour|minute|second)", r"1 \1", t)
    t = re.sub(r"\b(\d+) and a half (hours?|minutes?)", lambda m: f"{m.group(1)}.5 {m.group(2)}", t)
    t = re.sub(r"\b1 (hours?|minutes?) and a half\b", r"1.5 \1", t)
    unit = r"(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)\b"
    matches = list(re.finditer(r"(\d+(?:\.\d+)?)\s*" + unit, t))
    if not matches:
        return None
    total = 0.0
    start = matches[0].start()
    end = matches[0].end()
    prev_end = None
    for m in matches:
        if prev_end is not None and not re.fullmatch(r"\s*(and)?\s*", t[prev_end:m.start()]):
            break
        n = float(m.group(1))
        u = m.group(2)
        total += n * (3600 if u.startswith("h") else 60 if u.startswith("m") else 1)
        end = prev_end = m.end()
    # "1 hour 20" -> trailing bare number means minutes
    tail = re.match(r"\s*(and\s*)?(\d{1,2})\b(?!\s*(?:am|pm|:))", t[end:])
    if tail and matches[-1].group(2).startswith("h") and len(matches) == 1:
        total += int(tail.group(2)) * 60
        end += tail.end()
    return (int(round(total)), (start, end)) if total > 0 else None


def find_repeat(text: str) -> list[int] | None:
    t = text.lower()
    if re.search(r"\b(every ?day|daily|each day|every morning|every night|every evening)\b", t):
        return [0, 1, 2, 3, 4, 5, 6]
    if re.search(r"\b(weekdays?|every weekday|monday to friday|work ?days)\b", t):
        return [0, 1, 2, 3, 4]
    if re.search(r"\b(weekends?|every weekend|saturday and sunday)\b", t):
        return [5, 6]
    if re.search(r"\bevery\b", t):
        found = [i for i, d in enumerate(DAYS) if re.search(rf"\b{d}s?\b", t)]
        if found:
            return found
    return None


def find_date(text: str, now: datetime) -> str | None:
    """'tomorrow' or a named weekday (next occurrence) -> YYYY-MM-DD; None = soonest."""
    t = text.lower()
    if re.search(r"\b(tomorrow|tmrw)\b", t):
        return (now + timedelta(days=1)).strftime("%Y-%m-%d")
    if re.search(r"\bevery\b", t):
        return None
    for i, d in enumerate(DAYS):
        if re.search(rf"\b(on )?{d}\b", t):
            ahead = (i - now.weekday()) % 7
            ahead = ahead or 7
            return (now + timedelta(days=ahead)).strftime("%Y-%m-%d")
    return None
