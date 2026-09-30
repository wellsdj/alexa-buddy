"""Fake Groq / ElevenLabs / Open-Meteo at the HTTP layer, so tests exercise the real
request building and response parsing without network access or API keys."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx


def make_forecast(now: datetime, rain: dict[int, int] | None = None, days: int = 7,
                  current_code: int = 3, temp: float = 14.2) -> dict:
    """Open-Meteo shaped response. rain maps hour-of-today -> precipitation probability."""
    rain = rain or {}
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    hourly = {"time": [], "temperature_2m": [], "precipitation_probability": [], "precipitation": [],
              "weather_code": []}
    for h in range(24 * days):
        t = start + timedelta(hours=h)
        p = rain.get(h, 5) if h < 24 else 10
        hourly["time"].append(t.strftime("%Y-%m-%dT%H:%M"))
        hourly["temperature_2m"].append(12 + (h % 24) / 4)
        hourly["precipitation_probability"].append(p)
        hourly["precipitation"].append(0.6 if p >= 60 else 0.0)
        hourly["weather_code"].append(61 if p >= 60 else 3)
    daily = {"time": [], "weather_code": [], "temperature_2m_max": [], "temperature_2m_min": [],
             "precipitation_probability_max": [], "precipitation_sum": [], "sunrise": [], "sunset": []}
    for d in range(days):
        day = start + timedelta(days=d)
        daily["time"].append(day.strftime("%Y-%m-%d"))
        daily["weather_code"].append(61 if d == 0 and rain else 2)
        daily["temperature_2m_max"].append(17.4 - d * 0.3)
        daily["temperature_2m_min"].append(8.6 + d * 0.2)
        daily["precipitation_probability_max"].append(max(rain.values()) if d == 0 and rain else 10)
        daily["precipitation_sum"].append(2.1 if d == 0 and rain else 0.0)
        daily["sunrise"].append(day.strftime("%Y-%m-%dT07:01"))
        daily["sunset"].append(day.strftime("%Y-%m-%dT18:40"))
    return {
        "latitude": 51.46, "longitude": -0.3, "timezone": "Europe/London",
        "current": {"time": now.strftime("%Y-%m-%dT%H:%M"), "temperature_2m": temp, "apparent_temperature": temp - 1,
                    "weather_code": current_code, "precipitation": 0.0, "wind_speed_10m": 9.0,
                    "relative_humidity_2m": 70},
        "hourly": hourly, "daily": daily,
    }


def chat_response(model: str, content: str = "", tool_calls: list | None = None) -> dict:
    msg = {"role": "assistant", "content": content}
    if tool_calls:
        msg["tool_calls"] = [
            {"id": f"call_{i}", "type": "function",
             "function": {"name": name, "arguments": json.dumps(args)}}
            for i, (name, args) in enumerate(tool_calls)]
    return {"id": "chatcmpl-test", "object": "chat.completion", "model": model,
            "choices": [{"index": 0, "message": msg, "finish_reason": "tool_calls" if tool_calls else "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class FakeAPIs:
    def __init__(self):
        self.models = ["openai/gpt-oss-20b", "openai/gpt-oss-120b", "qwen/qwen3.6-27b", "whisper-large-v3-turbo"]
        self.transcripts: list[str] = []
        self.chat_script: list = []  # callables(body) -> (status, json, headers) or dicts
        self.chat_requests: list[dict] = []
        self.stt_requests: list[dict] = []
        self.tts_requests: list[dict] = []
        self.forecast = make_forecast(datetime.now(ZoneInfo("Europe/London")))
        self.tts_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.groq.com" in url and url.endswith("/models"):
            if request.headers.get("authorization") == "Bearer gsk_wrong":
                return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
            return httpx.Response(200, json={"data": [{"id": m, "active": True} for m in self.models]})
        if "audio/transcriptions" in url:
            body = request.content.decode(errors="ignore")
            self.stt_requests.append({"has_wav": "RIFF" in body, "model": "whisper" in body})
            text = self.transcripts.pop(0) if self.transcripts else ""
            return httpx.Response(200, json={"text": text})
        if "chat/completions" in url:
            body = json.loads(request.content)
            self.chat_requests.append(body)
            step = self.chat_script.pop(0) if self.chat_script else chat_response(body["model"], "Okay.")
            if callable(step):
                status, payload, headers = step(body)
                return httpx.Response(status, json=payload, headers=headers)
            return httpx.Response(200, json=step, headers={"x-ratelimit-remaining-requests": "999"})
        if "api.elevenlabs.io" in url and "text-to-speech" in url:
            body = json.loads(request.content)
            self.tts_requests.append({"url": url, "body": body})
            if self.tts_status != 200:
                return httpx.Response(self.tts_status, json={"detail": {"status": "quota_exceeded"}})
            # 50 ms of audio per character, a quiet 440 Hz tone so it isn't pure zeros
            import numpy as np
            n = 800 * max(1, len(body["text"]))
            tone = (np.sin(np.arange(n) * 2 * np.pi * 440 / 16000) * 2000).astype(np.int16)
            return httpx.Response(200, content=tone.tobytes())
        if "api.elevenlabs.io" in url and "subscription" in url:
            return httpx.Response(200, json={"character_count": 1200, "character_limit": 10000,
                                             "next_character_count_reset_unix": 0, "tier": "free"})
        if "geocoding-api.open-meteo.com" in url:
            return httpx.Response(200, json={"results": [
                {"name": "Richmond", "admin2": "Greater London", "country": "United Kingdom",
                 "latitude": 51.46, "longitude": -0.3, "timezone": "Europe/London"}]})
        if "api.open-meteo.com" in url:
            return httpx.Response(200, json=self.forecast)
        return httpx.Response(404, json={"error": "unexpected " + url})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
