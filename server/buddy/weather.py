"""Weather from Open-Meteo (free, no key). Cached for 10 minutes.

Turns the raw forecast into things worth saying out loud: what it's like now, today's
high and low, and whether it will rain and roughly when.
"""
from __future__ import annotations

import logging
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
        self.last_error = ""

    def _where(self) -> tuple:
        s = self.settings
        return (round(float(s["latitude"]), 4), round(float(s["longitude"]), 4), s["timezone"])

    async def fetch(self, force: bool = False) -> dict | None:
        where = self._where()
        if not force and self._cache and self._cache[1] == where and time.time() - self._cache[0] < 600:
            return self._cache[2]
        params = {
            "latitude": where[0], "longitude": where[1], "timezone": where[2],
            "current": "temperature_2m,apparent_temperature,weather_code,precipitation,wind_speed_10m,relative_humidity_2m",
            "hourly": "temperature_2m,precipitation_probability,precipitation,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,sunrise,sunset",
            "forecast_days": 7,
            "wind_speed_unit": "mph",
            "temperature_unit": self.settings["temperature_unit"],
        }
        try:
            r = await self.client.get(FORECAST_URL, params=params)
            r.raise_for_status()
            data = r.json()
            self._cache = (time.time(), where, data)
            self.last_error = ""
            return data
        except (httpx.HTTPError, ValueError) as e:
            self.last_error = str(e)
            log.warning("weather fetch failed: %s", e)
            # A stale forecast beats no forecast.
            return self._cache[2] if self._cache else None

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

    def summarize(self, data: dict, day: str = "today", now: datetime | None = None) -> dict:
        tz = ZoneInfo(self.settings["timezone"])
        now = now or datetime.now(tz)
        cur = data.get("current", {})
        idx = {"today": 0, "tomorrow": 1}.get(day, 0)
        d = self._day(data, idx, now)
        place = self.settings["location_name"].split(",")[0]
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
            parts.append(f"Tomorrow in {place}: {d['condition']}, with a high of {d['high']} and a low of {d['low']}.")
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
            "place": self.settings["location_name"],
        }

    async def spoken(self, day: str = "today") -> str:
        data = await self.fetch()
        if not data:
            return "I can't reach the weather service right now."
        return self.summarize(data, day)["spoken"]

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
