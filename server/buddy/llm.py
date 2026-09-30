"""Groq chat client with model discovery, per-model cooldowns and automatic fallback.

The free tier gives each model its own budget (about 30 req/min, 8k tokens/min and
1,000 req/day per model as of Sept 2026), so falling through a list of models when one
hits a limit is what keeps the assistant answering.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from .config import Settings

log = logging.getLogger("buddy.llm")
GROQ_BASE = "https://api.groq.com/openai/v1"


class LLMError(Exception):
    pass


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    usage: dict = field(default_factory=dict)
    ms: int = 0

    @property
    def assistant_message(self) -> dict:
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


def _parse_duration(value: str | None) -> float:
    """Groq reset headers look like '7.66s', '2m59.56s' or '1h2m3s'."""
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        pass
    total = 0.0
    for num, unit in re.findall(r"([\d.]+)(ms|h|m|s)", value):
        total += float(num) * {"h": 3600, "m": 60, "s": 1, "ms": 0.001}[unit]
    return total


class GroqLLM:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=5.0))
        self.available_models: set[str] | None = None
        self.discovered_at = 0.0
        self.cooldown: dict[str, float] = {}
        self.dead: set[str] = set()
        self.limits: dict[str, dict] = {}
        self.last_error = ""

    def _key(self) -> str:
        key = self.settings.secret("GROQ_API_KEY")
        if not key:
            raise LLMError("No Groq API key set")
        return key

    async def discover(self, force: bool = False) -> set[str] | None:
        if not force and self.available_models is not None and time.time() - self.discovered_at < 6 * 3600:
            return self.available_models
        try:
            self.discover_status = None
            r = await self.client.get(f"{GROQ_BASE}/models", headers={"Authorization": f"Bearer {self._key()}"})
            self.discover_status = r.status_code
            if r.status_code == 200:
                ids = {m["id"] for m in r.json().get("data", []) if m.get("active", True)}
                self.available_models = ids
                self.discovered_at = time.time()
                self.dead -= ids
                log.info("Groq models available: %s", ", ".join(sorted(ids)))
        except (httpx.HTTPError, LLMError) as e:
            log.info("model discovery failed: %s", e)
        return self.available_models

    def candidates(self, tier: str) -> list[str]:
        first = self.settings["smart_model"] if tier == "smart" else self.settings["fast_model"]
        order = [first] + list(self.settings["fallback_models"])
        if tier == "fast":
            order.append(self.settings["smart_model"])
        seen, out = set(), []
        for m in order:
            if not m or m in seen or m in self.dead:
                continue
            seen.add(m)
            if self.available_models is not None and m not in self.available_models:
                continue
            out.append(m)
        if not out and self.available_models:
            # Every preferred model has been retired: take whatever chat model exists.
            out = sorted(m for m in self.available_models
                         if not any(t in m for t in ("whisper", "tts", "guard", "orpheus", "safeguard")))
        return out

    def _params(self, model: str, tier: str) -> dict:
        if model.startswith("openai/gpt-oss"):
            return {"reasoning_effort": "medium" if tier == "smart" else "low", "include_reasoning": False}
        if model.startswith("qwen/"):
            return {"reasoning_effort": "default" if tier == "smart" else "none", "include_reasoning": False}
        return {}

    def _note_limits(self, model: str, headers: httpx.Headers) -> None:
        info = self.limits.setdefault(model, {})
        for h in ("x-ratelimit-remaining-requests", "x-ratelimit-remaining-tokens",
                  "x-ratelimit-limit-requests", "x-ratelimit-limit-tokens"):
            if h in headers:
                info[h.replace("x-ratelimit-", "")] = headers[h]
        info["at"] = time.time()

    async def chat(self, messages: list[dict], *, tier: str = "fast", tools: list[dict] | None = None,
                   json_mode: bool = False, max_tokens: int = 600, temperature: float = 0.6,
                   builtin_tools: list[dict] | None = None) -> ChatResult:
        key = self._key()
        await self.discover()
        errors = []
        now = time.time()
        models = self.candidates(tier)
        ready = [m for m in models if self.cooldown.get(m, 0) <= now]
        if not ready and models:
            # Everything is cooling down: wait for the soonest one if it's close.
            soonest = min(models, key=lambda m: self.cooldown.get(m, 0))
            wait = self.cooldown[soonest] - now
            if wait <= 6:
                await asyncio.sleep(wait)
                ready = [soonest]
        for model in ready:
            body: dict[str, Any] = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_completion_tokens": max_tokens,
                **self._params(model, tier),
            }
            all_tools = (tools or []) + (builtin_tools or [])
            if all_tools:
                body["tools"] = all_tools
                body["tool_choice"] = "auto"
            if json_mode:
                body["response_format"] = {"type": "json_object"}
            for attempt in range(3):
                t0 = time.monotonic()
                try:
                    r = await self.client.post(
                        f"{GROQ_BASE}/chat/completions",
                        headers={"Authorization": f"Bearer {key}"}, json=body)
                except httpx.HTTPError as e:
                    errors.append(f"{model}: network {e}")
                    break
                ms = int((time.monotonic() - t0) * 1000)
                self._note_limits(model, r.headers)
                if r.status_code == 200:
                    j = r.json()
                    msg = j["choices"][0]["message"]
                    return ChatResult(
                        content=(msg.get("content") or "").strip(),
                        tool_calls=msg.get("tool_calls") or [],
                        model=model, usage=j.get("usage", {}), ms=ms)
                text = r.text[:400]
                if r.status_code == 429:
                    retry = _parse_duration(r.headers.get("retry-after")) or \
                        _parse_duration(r.headers.get("x-ratelimit-reset-tokens")) or 10
                    if "per day" in text or "RPD" in text or "TPD" in text:
                        retry = max(retry, 600)
                    self.cooldown[model] = time.time() + retry
                    errors.append(f"{model}: rate limited ({retry:.0f}s)")
                    log.warning("%s rate limited for %.0fs", model, retry)
                    break
                if r.status_code in (404,) or "decommissioned" in text or "model_not_found" in text \
                        or "does not exist" in text:
                    self.dead.add(model)
                    errors.append(f"{model}: unavailable")
                    break
                if r.status_code == 400 and attempt == 0 and any(
                        p in text for p in ("reasoning_effort", "include_reasoning", "response_format")):
                    # This model doesn't take one of the optional knobs: drop them and retry.
                    for p in ("reasoning_effort", "include_reasoning", "response_format"):
                        body.pop(p, None)
                    continue
                if r.status_code == 400 and "tool_use_failed" in text and attempt < 1:
                    continue  # the model produced a malformed tool call; one retry usually fixes it
                if r.status_code >= 500 and attempt < 1:
                    await asyncio.sleep(0.5)
                    continue
                errors.append(f"{model}: HTTP {r.status_code} {text[:160]}")
                break
        self.last_error = "; ".join(errors) or "no model available"
        raise LLMError(self.last_error)

    async def web_search(self, query: str) -> str:
        """Groq's built-in browser search (gpt-oss models) for anything time-sensitive."""
        messages = [
            {"role": "system", "content": "Search the web and answer in 2-4 plain sentences suitable for "
                                          "reading aloud. Include dates and numbers that matter. No links."},
            {"role": "user", "content": query},
        ]
        for tier in ("smart", "fast"):
            try:
                res = await self.chat(messages, tier=tier, builtin_tools=[{"type": "browser_search"}],
                                      max_tokens=700, temperature=0.3)
                if res.content:
                    return res.content
            except LLMError as e:
                log.info("web search via %s failed: %s", tier, e)
        return "Web search isn't available right now."

    def status(self) -> dict:
        now = time.time()
        return {
            "available": sorted(self.available_models) if self.available_models else None,
            "fast": self.candidates("fast")[:1],
            "smart": self.candidates("smart")[:1],
            "cooldowns": {m: round(t - now) for m, t in self.cooldown.items() if t > now},
            "limits": self.limits,
            "last_error": self.last_error,
        }


def parse_json_loose(text: str) -> Any:
    """Models sometimes wrap JSON in prose or code fences."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}|\[.*\]", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None
