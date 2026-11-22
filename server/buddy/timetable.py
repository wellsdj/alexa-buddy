"""Your school timetable: Week A and Week B, read from photos, so Buddy knows your day without going online."""
from __future__ import annotations

import base64
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
READ_PROMPT = """This is a photo of a school timetable for one week (one fortnight's "Week {week}").
Read every lesson. Reply with JSON only:
{{"Monday": [{{"start": "08:50", "end": "09:50", "subject": "Maths", "teacher": "Mr Smith", "room": "M4"}}], "Tuesday": [...], ...}}
Monday to Friday, lessons in order. Leave out breaks, lunch and registration unless they're lessons. Use 24h
times if shown (empty if not). Empty strings for anything not on the timetable. If the photo holds both weeks,
read only Week {week}."""


DEFAULT = Path(__file__).with_name("timetable_default.json")  # your Week A / Week B, five periods a day


def _lessons(day: list) -> list[dict]:
    """A day is a list of subjects (or lessons read from a photo)."""
    return [{"subject": x} if isinstance(x, str) else x for x in day]


class Timetable:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "timetable.json"

    def load(self) -> dict:
        for path in (self.path, DEFAULT):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            weeks = {w: {d: _lessons(v) for d, v in days.items()} for w, days in data.get("weeks", {}).items()}
            if weeks:
                return {**data, "weeks": weeks}
        return {"weeks": {}, "week_a_monday": ""}

    def save(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))

    def set_week(self, week: str, days: dict) -> None:
        data = self.load()
        data.setdefault("weeks", {})[week.upper()] = {d: days.get(d, []) for d in DAYS}
        self.save(data)

    def set_this_week(self, week: str, today: date) -> str:
        """'This week is week B': remembers which Monday was a Week A, so every later week follows."""
        week = week.strip().upper()[:1]
        if week not in ("A", "B"):
            raise ValueError("Week A or week B?")
        monday = today - timedelta(days=today.weekday())
        if today.weekday() >= 5:  # said at the weekend: "this week" is the one about to start
            monday += timedelta(days=7)
        if week == "B":
            monday -= timedelta(days=7)
        data = self.load()
        data["week_a_monday"] = monday.isoformat()
        self.save(data)  # from now on this file (with the timetable in it) is the one used
        return week

    def week_of(self, day: date) -> str:
        start = self.load().get("week_a_monday")
        if not start:
            return ""
        weeks = (day - day.fromisoformat(start)).days // 7
        return "A" if weeks % 2 == 0 else "B"

    def day(self, day: date, week: str = "") -> tuple[str, list[dict]]:
        week = (week or self.week_of(day)).upper()
        lessons = self.load().get("weeks", {}).get(week, {}).get(DAYS[day.weekday()], []) if day.weekday() < 5 else []
        return week, lessons

    def spoken(self, day: date, week: str = "") -> str:
        from .school_calendar import SchoolCalendar
        data = self.load()
        if not data.get("weeks"):
            return "No timetable saved yet."
        cal = SchoolCalendar(self.path.parent)
        why = cal.why_off(day)
        if why:
            if why == "the weekend":
                return f"{day:%A} is the weekend, so no lessons."
            return f"{day:%A} is {why}, so no school. You're back on {cal.next_school_day(day):%A %d %B}."
        if not week and not data.get("week_a_monday"):
            return "I don't know yet whether this is week A or week B: ask them, then call timetable with this_week."
        wk, lessons = self.day(day, week)
        if not lessons:
            return f"Nothing saved for {day:%A} in week {wk}."
        # Slowly, one period at a time: just the subjects. Form comes first.
        periods = " ".join(f"Period {i}, {l.get('subject', '')}." for i, l in enumerate(lessons, 1))
        last = " And it's the last day of term." if cal.last_day_of_term(day) else ""
        return f"{day:%A}, week {wk}. Form first. {periods}{last}"


async def read_photo(llm, image: bytes, media_type: str, week: str) -> dict:
    """Claude reads the timetable photo into lessons."""
    from .llm import parse_json_loose
    api = llm._api()
    r = await api.messages.create(
        model=llm.model("smart"), max_tokens=8000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                         "data": base64.b64encode(image).decode()}},
            {"type": "text", "text": READ_PROMPT.format(week=week)}]}])
    text = "".join(b.text for b in r.content if b.type == "text")
    data = parse_json_loose(text)
    if not isinstance(data, dict):
        raise ValueError("Couldn't read a timetable in that photo. Try a clearer, straight-on photo.")
    return {d: [l for l in data.get(d, []) if isinstance(l, dict)] for d in DAYS}


def which_day(text: str, now: datetime) -> date:
    t = (text or "today").lower()
    if "tomorrow" in t:
        return (now + timedelta(days=1)).date()
    if "yesterday" in t:
        return (now - timedelta(days=1)).date()
    for i, d in enumerate(DAYS + ["Saturday", "Sunday"]):
        if d.lower() in t:
            ahead = (i - now.weekday()) % 7
            if "next" in t and ahead == 0:
                ahead = 7
            return (now + timedelta(days=ahead)).date()
    m = re.search(r"\d{4}-\d{2}-\d{2}", t)
    if m:
        return date.fromisoformat(m.group())
    return now.date()
