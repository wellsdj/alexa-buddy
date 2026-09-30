"""Alarms, timers and reminders.

Repeating alarms are stored as a local wall-clock time plus weekdays, so they keep
firing at 7:00 across the clock changes. One-shots are stored as an absolute moment.
The ESP32 is sent the next week of fire times as plain UTC epochs, so it can ring on
its own even when the server is off; it never has to know about time zones.
"""
from __future__ import annotations

import json
import secrets
import threading
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
REPEAT_PRESETS = {
    "daily": [0, 1, 2, 3, 4, 5, 6],
    "everyday": [0, 1, 2, 3, 4, 5, 6],
    "weekdays": [0, 1, 2, 3, 4],
    "weekends": [5, 6],
    "none": [],
    "once": [],
    "": [],
}


@dataclass
class Alarm:
    id: str
    kind: str = "alarm"  # alarm | timer | reminder
    label: str = ""
    at: float = 0.0  # one-shots: UTC epoch
    time: str = ""  # repeating: "HH:MM" local
    days: list[int] = field(default_factory=list)  # 0=Mon ... 6=Sun
    enabled: bool = True
    created: float = field(default_factory=_time.time)
    duration: int = 0  # timers: original length in seconds

    @property
    def repeating(self) -> bool:
        return bool(self.days)


def parse_days(repeat: str | list | None) -> list[int]:
    if repeat is None:
        return []
    if isinstance(repeat, list):
        return sorted({int(d) for d in repeat if 0 <= int(d) <= 6})
    key = repeat.strip().lower().replace(" ", "").replace("-", "")
    if key in REPEAT_PRESETS:
        return REPEAT_PRESETS[key]
    days = set()
    for part in repeat.lower().replace("and", ",").replace("/", ",").split(","):
        part = part.strip()
        for i, name in enumerate(DAY_NAMES):
            if part and name.lower().startswith(part[:3]):
                days.add(i)
    return sorted(days)


def spoken_time(dt: datetime) -> str:
    h, m = dt.hour, dt.minute
    suffix = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    if h == 0 and m == 0:
        return "midnight"
    if h == 12 and m == 0:
        return "noon"
    return f"{h12} {suffix}" if m == 0 else f"{h12}:{m:02d} {suffix}"


def describe_days(days: list[int]) -> str:
    if days == REPEAT_PRESETS["daily"]:
        return "every day"
    if days == REPEAT_PRESETS["weekdays"]:
        return "every weekday"
    if days == REPEAT_PRESETS["weekends"]:
        return "every weekend"
    if len(days) == 1:
        return "every " + DAY_NAMES[days[0]]
    names = [DAY_NAMES[d] + "s" for d in days]
    return "on " + ", ".join(names[:-1]) + " and " + names[-1]


def human_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    if s and not h:
        parts.append(f"{s} second{'s' if s != 1 else ''}")
    return " and ".join(parts) if parts else "0 seconds"


class AlarmStore:
    def __init__(self, path: Path, tz_name: str = "Europe/London"):
        self.path = Path(path)
        self.tz = ZoneInfo(tz_name)
        self._lock = threading.RLock()
        self._items: dict[str, Alarm] = {}
        self._listeners: list = []
        self.recently_fired: list[tuple[float, Alarm]] = []
        self._load()

    def set_timezone(self, tz_name: str) -> None:
        self.tz = ZoneInfo(tz_name)
        self._changed()

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _load(self) -> None:
        if self.path.exists():
            try:
                for raw in json.loads(self.path.read_text()):
                    a = Alarm(**{k: v for k, v in raw.items() if k in Alarm.__dataclass_fields__})
                    self._items[a.id] = a
            except (json.JSONDecodeError, TypeError):
                pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(a) for a in self._items.values()], indent=2))
        tmp.replace(self.path)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            fn()

    def now_local(self, now: float | None = None) -> datetime:
        return datetime.fromtimestamp(now if now is not None else _time.time(), self.tz)

    # ---------- creation ----------
    def _new_id(self) -> str:
        while True:
            i = secrets.token_hex(2)
            if i not in self._items:
                return i

    def next_time_for(self, hhmm: str, now: float | None = None, date: str | None = None) -> float:
        """Next moment the local clock reads hh:mm (today if still ahead, else tomorrow)."""
        h, m = (int(x) for x in hhmm.split(":"))
        local_now = self.now_local(now)
        if date:
            d = datetime.strptime(date, "%Y-%m-%d").date()
            target = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz)
            return target.timestamp()
        target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target.timestamp() <= local_now.timestamp() + 1:
            nd = (local_now + timedelta(days=1)).date()
            target = datetime(nd.year, nd.month, nd.day, h, m, tzinfo=self.tz)
        return target.timestamp()

    def add_alarm(self, hhmm: str, repeat: str | list | None = None, label: str = "",
                  date: str | None = None, kind: str = "alarm", now: float | None = None) -> Alarm:
        h, m = (int(x) for x in hhmm.split(":"))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError("time out of range")
        hhmm = f"{h:02d}:{m:02d}"
        days = parse_days(repeat)
        with self._lock:
            a = Alarm(id=self._new_id(), kind=kind, label=label.strip())
            if days and not date:
                a.time, a.days = hhmm, days
            else:
                a.at = self.next_time_for(hhmm, now, date)
            self._items[a.id] = a
            self._save()
        self._changed()
        return a

    def add_timer(self, seconds: int, label: str = "", now: float | None = None, kind: str = "timer") -> Alarm:
        if seconds <= 0 or seconds > 24 * 3600:
            raise ValueError("timer must be between 1 second and 24 hours")
        now = now if now is not None else _time.time()
        with self._lock:
            a = Alarm(id=self._new_id(), kind=kind, label=label.strip(), at=now + seconds, duration=seconds)
            self._items[a.id] = a
            self._save()
        self._changed()
        return a

    def add_reminder_in(self, seconds: int, text: str, now: float | None = None) -> Alarm:
        now = now if now is not None else _time.time()
        with self._lock:
            a = Alarm(id=self._new_id(), kind="reminder", label=text.strip(), at=now + seconds)
            self._items[a.id] = a
            self._save()
        self._changed()
        return a

    def snooze(self, alarm: Alarm | None, minutes: int, now: float | None = None) -> Alarm:
        now = now if now is not None else _time.time()
        label = alarm.label if alarm else ""
        with self._lock:
            a = Alarm(id=self._new_id(), kind="alarm", label=label, at=now + minutes * 60)
            self._items[a.id] = a
            self._save()
        self._changed()
        return a

    # ---------- queries ----------
    def get(self, alarm_id: str) -> Alarm | None:
        with self._lock:
            return self._items.get(alarm_id)

    def list(self) -> list[Alarm]:
        with self._lock:
            return list(self._items.values())

    def next_fire(self, a: Alarm, now: float | None = None) -> float | None:
        if not a.enabled:
            return None
        now = now if now is not None else _time.time()
        if not a.repeating:
            return a.at if a.at > now - 60 else None
        h, m = (int(x) for x in a.time.split(":"))
        base = self.now_local(now).date()
        for offset in range(0, 9):
            d = base + timedelta(days=offset)
            if d.weekday() not in a.days:
                continue
            ts = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz).timestamp()
            if ts > now:
                return ts
        return None

    def upcoming(self, now: float | None = None, horizon_s: float = 8 * 86400, limit: int = 48) -> list[tuple[float, Alarm]]:
        now = now if now is not None else _time.time()
        out: list[tuple[float, Alarm]] = []
        with self._lock:
            items = list(self._items.values())
        for a in items:
            if not a.enabled:
                continue
            if not a.repeating:
                if now - 60 < a.at <= now + horizon_s:
                    out.append((a.at, a))
                continue
            h, m = (int(x) for x in a.time.split(":"))
            base = self.now_local(now).date()
            for offset in range(0, int(horizon_s // 86400) + 2):
                d = base + timedelta(days=offset)
                if d.weekday() in a.days:
                    ts = datetime(d.year, d.month, d.day, h, m, tzinfo=self.tz).timestamp()
                    if now < ts <= now + horizon_s:
                        out.append((ts, a))
        out.sort(key=lambda x: x[0])
        return out[:limit]

    def device_schedule(self, now: float | None = None) -> list[dict]:
        return [{"id": a.id, "at": int(ts), "kind": a.kind} for ts, a in self.upcoming(now)]

    def describe(self, a: Alarm, now: float | None = None) -> str:
        now = now if now is not None else _time.time()
        if a.kind == "timer":
            left = max(0, a.at - now)
            name = f"{a.label} timer" if a.label else "timer"
            return f"a {name} with {human_duration(left)} left"
        if a.repeating:
            h, m = (int(x) for x in a.time.split(":"))
            t = spoken_time(datetime(2000, 1, 1, h, m))
            what = "reminder to " + a.label if a.kind == "reminder" and a.label else (
                f"{a.label} alarm" if a.label else "alarm")
            return f"{what} at {t} {describe_days(a.days)}"
        when = self.now_local(a.at)
        today = self.now_local(now).date()
        if when.date() == today:
            day = "today"
        elif when.date() == today + timedelta(days=1):
            day = "tomorrow"
        else:
            day = f"on {DAY_NAMES[when.weekday()]} the {ordinal(when.day)}"
        if a.kind == "reminder":
            return f"a reminder at {spoken_time(when)} {day} to {a.label}"
        name = f"{a.label} alarm" if a.label else "alarm"
        article = "an" if name[0].lower() in "aeiou" else "a"
        return f"{article} {name} for {spoken_time(when)} {day}"

    def confirm_text(self, a: Alarm, now: float | None = None) -> str:
        now = now if now is not None else _time.time()
        if a.kind == "timer":
            name = f"{a.label} timer" if a.label else "Timer"
            return f"{name[0].upper() + name[1:]} set for {human_duration(a.duration or (a.at - now))}."
        if a.repeating:
            h, m = (int(x) for x in a.time.split(":"))
            t = spoken_time(datetime(2000, 1, 1, h, m))
            nxt = self.next_fire(a, now)
            first = ""
            if nxt:
                d = self.now_local(nxt).date()
                today = self.now_local(now).date()
                first = (" The first one is today." if d == today else
                         " The first one is tomorrow." if d == today + timedelta(days=1) else
                         f" The first one is {DAY_NAMES[d.weekday()]}.")
            if a.kind == "reminder":
                return f"Okay, I'll remind you to {a.label} at {t} {describe_days(a.days)}.{first}"
            return f"Alarm set for {t} {describe_days(a.days)}.{first}"
        when = self.now_local(a.at)
        today = self.now_local(now).date()
        day = "today" if when.date() == today else (
            f"tomorrow, {DAY_NAMES[when.weekday()]}" if when.date() == today + timedelta(days=1)
            else f"{DAY_NAMES[when.weekday()]} the {ordinal(when.day)}")
        left = human_duration(a.at - now)
        if a.kind == "reminder":
            return f"Okay, I'll remind you to {a.label} at {spoken_time(when)} {day}."
        return f"Alarm set for {spoken_time(when)} {day}. That's {left} from now."

    # ---------- changes ----------
    def cancel(self, alarm_id: str) -> Alarm | None:
        with self._lock:
            a = self._items.pop(alarm_id, None)
            if a:
                self._save()
        if a:
            self._changed()
        return a

    def cancel_all(self, kind: str | None = None) -> int:
        with self._lock:
            ids = [i for i, a in self._items.items() if kind in (None, "all", a.kind)]
            for i in ids:
                self._items.pop(i)
            if ids:
                self._save()
        if ids:
            self._changed()
        return len(ids)

    def set_enabled(self, alarm_id: str, enabled: bool) -> Alarm | None:
        with self._lock:
            a = self._items.get(alarm_id)
            if a:
                a.enabled = enabled
                self._save()
        if a:
            self._changed()
        return a

    def fired(self, alarm_id: str, now: float | None = None) -> Alarm | None:
        """Record that something rang; one-shots are removed so they can't ring twice."""
        now = now if now is not None else _time.time()
        with self._lock:
            a = self._items.get(alarm_id)
            if not a:
                return None
            self.recently_fired = [(t, x) for t, x in self.recently_fired if now - t < 3600]
            self.recently_fired.append((now, a))
            if not a.repeating:
                self._items.pop(alarm_id, None)
                self._save()
        if not a.repeating:
            self._changed()
        return a

    def last_fired(self, within_s: float = 900, now: float | None = None) -> Alarm | None:
        now = now if now is not None else _time.time()
        for t, a in reversed(self.recently_fired):
            if now - t < within_s:
                return a
        return None

    def cleanup(self, now: float | None = None) -> list[Alarm]:
        """Drop one-shots whose time passed more than 10 minutes ago without a report."""
        now = now if now is not None else _time.time()
        with self._lock:
            stale = [a for a in self._items.values() if not a.repeating and a.at < now - 600]
            for a in stale:
                self._items.pop(a.id, None)
            if stale:
                self._save()
        if stale:
            self._changed()
        return stale

    def to_api(self, now: float | None = None) -> list[dict]:
        now = now if now is not None else _time.time()
        out = []
        for a in sorted(self.list(), key=lambda x: self.next_fire(x, now) or 9e18):
            nxt = self.next_fire(a, now)
            d = asdict(a)
            d["next"] = nxt
            d["next_local"] = self.now_local(nxt).strftime("%a %d %b %H:%M") if nxt else None
            d["description"] = self.describe(a, now)
            out.append(d)
        return out


def ordinal(n: int) -> str:
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"
