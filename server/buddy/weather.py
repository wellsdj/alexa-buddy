"""Weather from Open-Meteo (free, no key). Cached for 10 minutes.

Turns the raw forecast into things worth saying out loud: what it's like now, today's
high and low, and whether it will rain and roughly when.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

from .config import Settings

log = logging.getLogger("buddy.weather")
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

WMO = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "foggy with frost",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorms", 96: "thunderstorms with hail", 99: "severe thunderstorms with hail",
}
WET_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}
LIKELY, POSSIBLE, WET_MM = 50, 30, 0.3


DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
HOME_WORDS = {"", "home", "here", "my house", "my place", "richmond", "outside"}
NUMBERS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
           "ten": 10}


def day_indices(when: str, now: datetime) -> list[int] | None:
    """Which forecast days (0 = today) a phrase means. None = further ahead than 10 days."""
    t = (when or "").lower()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    if m:
        ahead = (datetime(int(m[1]), int(m[2]), int(m[3])).date() - now.date()).days
        return [ahead] if 0 <= ahead <= 10 else None
    m = re.search(r"\b(?:next|coming|following) (\d+|\w+) days\b|\b(\d+|\w+)[- ]day (?:forecast|outlook)", t)
    if m:
        n = m.group(1) or m.group(2)
        n = int(n) if n.isdigit() else NUMBERS.get(n, 7)
        if n > 10:
            return None
        return list(range(0, n + 1))
    if re.search(r"\b(fortnight|ten days|10 days|two weeks)\b", t):
        return list(range(0, 11))
    m = re.search(r"\bin (\d+|\w+) days?\b", t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS.get(m.group(1))
        if n is not None:
            return [n] if n <= 10 else None
    m = re.search(r"\bin (a|one|\d+|two|three|four) weeks?\b", t)
    if m:
        n = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4}.get(m.group(1)) or int(m.group(1))
        return [7] if n == 1 else None
    if re.search(r"\b(week|week ahead|7 days|seven days)\b", t):
        return list(range(0, 7))
    if "weekend" in t:
        if now.weekday() == 6:  # Sunday: "the weekend" is today, "next weekend" the coming one
            return [6, 7] if "next weekend" in t else [0]
        sat = (5 - now.weekday()) % 7
        if "next weekend" in t and now.weekday() >= 5:
            sat += 7
        return [i for i in (sat, sat + 1) if i <= 10] or None
    if "day after tomorrow" in t:
        return [2]
    if "tomorrow" in t:
        return [1]
    for i, name in enumerate(DAYS):
        if re.search(rf"\b{name.lower()}\b", t):
            ahead = (i - now.weekday()) % 7
            if ahead == 0 and re.search(rf"\bnext {name.lower()}\b", t):
                ahead = 7
            return [ahead]
    return [0]


def hour_label(h: int) -> str:
    if h == 0:
        return "midnight"
    if h == 12:
        return "midday"
    return f"{h % 12 or 12}{'am' if h < 12 else 'pm'}"


@dataclass
class RainWindow:
    start: int
    end: int  # hour the rain stops (exclusive)
    peak: int
    likely: bool

    def phrase(self) -> str:
        chance = f"up to {self.peak} percent" if self.peak else ""
        word = "likely" if self.likely else "possible"
        if self.end - self.start <= 1:
            core = f"rain is {word} around {hour_label(self.start)}"
        elif self.end >= 24:
            core = f"rain is {word} from {hour_label(self.start)} into the night"
        else:
            core = f"rain is {word} from {hour_label(self.start)} until about {hour_label(self.end)}"
        return f"{core}, {chance}" if chance else core


def rain_windows(hours: list[int], probs: list[float | None], mm: list[float | None]) -> list[RainWindow]:
    windows: list[RainWindow] = []
    cur: RainWindow | None = None
    for h, p, r in zip(hours, probs, mm):
        p = p or 0
        r = r or 0
        wet = p >= POSSIBLE or r >= WET_MM
        if wet:
            likely = p >= LIKELY or r >= WET_MM
            if cur and h == cur.end:
                cur.end = h + 1
                cur.peak = max(cur.peak, int(p))
                cur.likely = cur.likely or likely
            else:
                cur = RainWindow(h, h + 1, int(p), likely)
                windows.append(cur)
        else:
            cur = None
    # Merge windows separated by a single dry hour; that's one rainy spell to a listener.
    merged: list[RainWindow] = []
    for w in windows:
        if merged and w.start - merged[-1].end <= 1:
            m = merged[-1]
            m.end, m.peak, m.likely = w.end, max(m.peak, w.peak), m.likely or w.likely
        else:
            merged.append(w)
    return merged


class Weather:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._cache: tuple[float, tuple, dict] | None = None
        self._places: dict[tuple, tuple[float, dict]] = {}  # (lat, lon, tz) -> (fetched at, forecast)
        self.last_error = ""

    def _where(self) -> tuple:
        s = self.settings
        return (round(float(s["latitude"]), 4), round(float(s["longitude"]), 4), s["timezone"])

    async def fetch(self, force: bool = False, where: tuple | None = None) -> dict | None:
        """Forecast for home, or for where=(lat, lon, timezone). Today plus 10 days."""
        home = where is None
        where = where or self._where()
        hit = self._places.get(where)
        if not force and hit and time.time() - hit[0] < 600:
            return hit[1]
        params = {
            "latitude": where[0], "longitude": where[1], "timezone": where[2],
            "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m,relative_humidity_2m",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,"
                     "sunrise,sunset,wind_speed_10m_max",
            "forecast_days": 11,
            "wind_speed_unit": "mph",
            "temperature_unit": self.settings["temperature_unit"],
        }
        try:
            r = await self.client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
            self._places[where] = (time.time(), data)
            if home:
                self._cache = (time.time(), where, data)
                self.last_error = ""
            return data
        except (httpx.HTTPError, ValueError) as e:
            if home:
                self.last_error = str(e)
            log.warning("weather fetch failed: %s", e)
            # A stale forecast beats no forecast.
            return hit[1] if hit else None

    async def geocode(self, name: str) -> list[dict]:
        r = await self.client.get(GEOCODE_URL, params={"name": name, "count": 8, "language": "en", "format": "json"})
        r.raise_for_status()
        out = []
        for x in r.json().get("results", []) or []:
            parts = [x.get("name"), x.get("admin2") or x.get("admin1"), x.get("country")]
            out.append({
                "label": ", ".join(p for p in parts if p),
                "latitude": x["latitude"], "longitude": x["longitude"],
                "timezone": x.get("timezone") or self.settings["timezone"],
            })
        return out

    # ---------- interpretation ----------
    def _day(self, data: dict, index: int, now: datetime) -> dict:
        daily = data["daily"]
        date = daily["time"][index]
        hourly = data["hourly"]
        hours, probs, mm = [], [], []
        for t, p, r in zip(hourly["time"], hourly["precipitation_probability"], hourly["precipitation"]):
            if not t.startswith(date):
                continue
            h = int(t[11:13])
            if index == 0 and h < now.hour:
                continue
            hours.append(h)
            probs.append(p)
            mm.append(r)
        return {
            "date": date,
            "high": round(daily["temperature_2m_max"][index]),
            "low": round(daily["temperature_2m_min"][index]),
            "condition": WMO.get(daily["weather_code"][index], "mixed"),
            "rain_chance": daily["precipitation_probability_max"][index],
            "rain_mm": daily["precipitation_sum"][index],
            "windows": rain_windows(hours, probs, mm),
        }

    def summarize(self, data: dict, day: str = "today", now: datetime | None = None, place: str | None = None,
                  tz: str | None = None, idx: int | None = None) -> dict:
        tz = ZoneInfo(tz or self.settings["timezone"])
        now = now or datetime.now(tz)
        cur = data.get("current", {})
        idx = {"today": 0, "tomorrow": 1}.get(day, 0) if idx is None else idx
        d = self._day(data, idx, now)
        place = place or self.settings["location_name"].split(",")[0]
        unit = "degrees"
        parts = []
        if idx == 0 and cur:
            temp = round(cur.get("temperature_2m", 0))
            feels = round(cur.get("apparent_temperature", temp))
            cond = WMO.get(cur.get("weather_code"), "")
            line = f"Right now in {place} it's {temp} {unit}"
            if cond:
                line += f" and {cond}"
            if abs(feels - temp) >= 3:
                line += f", feeling more like {feels}"
            parts.append(line + ".")
            parts.append(f"Today's high is {d['high']} and the low is {d['low']}.")
        else:
            when = "Tomorrow" if idx == 1 else DAYS[datetime.strptime(d["date"], "%Y-%m-%d").weekday()]
            parts.append(f"{when} in {place}: {d['condition']}, with a high of {d['high']} and a low of {d['low']}.")
        raining_now = idx == 0 and (cur.get("weather_code") in WET_CODES or (cur.get("precipitation") or 0) > 0.1)
        wins = d["windows"]
        if raining_now:
            first = wins[0] if wins and wins[0].start <= now.hour + 1 else None
            if first and first.end < 24:
                parts.append(f"It's raining now and should ease off around {hour_label(first.end)}.")
            else:
                parts.append("It's raining now and looks set to continue.")
            later = [w for w in wins if first is None or w is not first]
            if later:
                parts.append("Later, " + later[0].phrase() + ".")
        elif wins:
            text = wins[0].phrase()
            parts.append(text[0].upper() + text[1:] + ".")
            if len(wins) > 1:
                parts.append("And again " + wins[1].phrase().replace("rain is likely ", "").replace("rain is possible ", "") + ".")
        else:
            parts.append("No rain expected" + (" for the rest of the day." if idx == 0 else "."))
        return {
            "spoken": " ".join(parts),
            "today": {k: v for k, v in d.items() if k != "windows"} | {
                "rain_windows": [w.__dict__ for w in wins]},
            "current": cur,
            "place": place,
        }

    async def spoken(self, day: str = "today") -> str:
        data = await self.fetch()
        if not data:
            return "I can't reach the weather service right now."
        return self.summarize(data, day)["spoken"]

    async def report(self, place: str = "", when: str = "today") -> str:
        """What the weather apps show, said out loud: any place (empty = home), any day up to 10 days
        ahead ("friday", "the weekend", "in 5 days", "this week", "next 10 days", "2026-11-08")."""
        where, label, tzname = None, None, self.settings["timezone"]
        if place and place.strip().lower() not in HOME_WORDS:
            try:
                found = await self.geocode(place.strip())
            except (httpx.HTTPError, ValueError):
                found = []
            if not found:
                return f"I couldn't find a place called {place}."
            f = found[0]
            where = (round(float(f["latitude"]), 4), round(float(f["longitude"]), 4), f["timezone"])
            label, tzname = f["label"].split(",")[0], f["timezone"]
        data = await self.fetch(where=where)
        if not data:
            return "I can't reach the weather service right now."
        now = datetime.now(ZoneInfo(tzname))
        days = day_indices(when, now)
        if days is None:
            return "I can only see the weather up to 10 days ahead."
        label = label or self.settings["location_name"].split(",")[0]
        if len(days) == 1:
            return self.summarize(data, now=now, place=label, tz=tzname, idx=days[0])["spoken"]
        d = data["daily"]
        bits = []
        for i in days:
            if i >= len(d["time"]):
                break
            name = "today" if i == 0 else "tomorrow" if i == 1 else DAYS[datetime.strptime(d["time"][i], "%Y-%m-%d").weekday()]
            rain = d["precipitation_probability_max"][i] or 0
            bit = f"{name} {WMO.get(d['weather_code'][i], 'mixed')}, {round(d['temperature_2m_max'][i])} and {round(d['temperature_2m_min'][i])}"
            if rain >= 40:
                bit += f", {rain} percent chance of rain"
            bits.append(bit)
        return f"In {label}: " + "; ".join(bits) + "."

    async def context_line(self) -> str:
        """Compact snapshot for the system prompt so Buddy always knows the weather."""
        data = await self.fetch()
        if not data:
            return "Weather: unavailable."
        tz = ZoneInfo(self.settings["timezone"])
        now = datetime.now(tz)
        cur = data.get("current", {})
        today = self._day(data, 0, now)
        tom = self._day(data, 1, now)
        rain_today = today["windows"][0].phrase() if today["windows"] else "no rain expected"
        rain_tom = tom["windows"][0].phrase() if tom["windows"] else "no rain expected"
        unit = "°F" if self.settings["temperature_unit"] == "fahrenheit" else "°C"
        return (f"Weather in {self.settings['location_name']}: now {round(cur.get('temperature_2m', 0))}{unit} "
                f"{WMO.get(cur.get('weather_code'), '')}; today high {today['high']} low {today['low']}, {rain_today}; "
                f"tomorrow {tom['condition']}, high {tom['high']} low {tom['low']}, {rain_tom}.")
