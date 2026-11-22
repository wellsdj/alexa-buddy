"""Christ's School term dates: which days are school days, half terms and the last day of term.

Built in: the usual Richmond upon Thames dates for 2026-27 (the school mostly follows the borough).
Buddy also reads the school's own calendar online every week (data/school_calendar.json), and
those dates win when it has them, including INSET days.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

# Days off (first, last), inclusive. Weekends are always off.
DEFAULT_OFF = [
    ("2026-10-26", "2026-10-30", "October half term"),
    ("2026-12-21", "2027-01-01", "the Christmas holidays"),
    ("2027-02-15", "2027-02-19", "February half term"),
    ("2027-03-26", "2027-04-09", "the Easter holidays"),
    ("2027-05-03", "2027-05-03", "the May bank holiday"),
    ("2027-05-31", "2027-06-04", "May half term"),
    ("2027-07-21", "2027-09-01", "the summer holidays"),
]


class SchoolCalendar:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "school_calendar.json"

    def off_days(self) -> list[tuple[date, date, str]]:
        rows = DEFAULT_OFF
        try:
            saved = json.loads(self.path.read_text()).get("off", [])
            if saved:
                rows = [(r["from"], r["to"], r.get("what", "a holiday")) for r in saved]
        except (OSError, json.JSONDecodeError, KeyError, TypeError, AttributeError):
            pass
        out = []
        for a, b, what in rows:
            try:
                out.append((date.fromisoformat(a), date.fromisoformat(b), what))
            except ValueError:
                continue
        return out

    def save(self, off: list[dict]) -> None:
        self.path.write_text(json.dumps({"off": off}, indent=2))

    def why_off(self, day: date) -> str:
        """'' on a school day; otherwise why there's no school."""
        if day.weekday() >= 5:
            return "the weekend"
        for a, b, what in self.off_days():
            if a <= day <= b:
                return what
        return ""

    def is_school_day(self, day: date) -> bool:
        return not self.why_off(day)

    def next_school_day(self, day: date) -> date:
        d = day + timedelta(days=1)
        for _ in range(120):
            if self.is_school_day(d):
                return d
            d += timedelta(days=1)
        return d

    def last_day_of_term(self, day: date) -> bool:
        """A school day followed by a holiday (not just a weekend)."""
        if not self.is_school_day(day):
            return False
        return (self.next_school_day(day) - day).days > 3

    def note(self, day: date) -> str:
        """A line for Buddy: today's/that day's school status."""
        why = self.why_off(day)
        if why and why != "the weekend":
            back = self.next_school_day(day)
            return f"No school ({why}); back on {back:%A %d %B}."
        if self.last_day_of_term(day):
            return "It's the last day of term."
        return ""
