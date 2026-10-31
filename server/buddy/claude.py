"""Claude (Anthropic) as Buddy's brain: chat with tools, web search, and a key check.

brain.py and memory.py build OpenAI-style messages (the common format). They're converted to the
Messages API here and replies are converted back, so nothing else needs to know which brain
answered. Claude's own reply blocks (thinking and tool calls) travel with the assistant message
under "_claude", so they go back unchanged in the next round of a tool loop, as the API requires.

Haiku thinks before answering (adaptive thinking); "effort" sets how hard: low for everyday
replies so they come back fast, high for big jobs. Sampling settings like temperature are not
sent: current models only accept their defaults.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field

try:
    import anthropic
except ImportError:  # an old install that hasn't been updated yet
    anthropic = None

from .config import Settings
from .llm import ChatResult, LLMError

log = logging.getLogger("buddy.claude")

EFFORT = {"fast": "low", "smart": "high"}
JSON_ONLY = "Reply with a single JSON object and nothing else."
NOT_INSTALLED = "the Claude library isn't installed yet: double-click Update Buddy"
SEARCH_SYSTEM = ("Search the web and answer in 2-4 plain sentences suitable for reading aloud. "
                 "Include dates and numbers that matter. No links.")


@dataclass
class ClaudeResult(ChatResult):
    blocks: list[dict] = field(default_factory=list)

    @property
    def assistant_message(self) -> dict:
        msg = super().assistant_message
        msg["_claude"] = self.blocks
        return msg


def convert_tools(tools: list[dict]) -> list[dict]:
    """OpenAI function tools -> Claude tools."""
    out = []
    for t in tools:
        f = t.get("function", t)
        out.append({"name": f["name"], "description": f.get("description", ""),
                    "input_schema": f.get("parameters") or {"type": "object", "properties": {}}})
    return out


def convert_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    """OpenAI-style messages -> (system prompt, Claude messages)."""
    system: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system":
            system.append(m.get("content") or "")
        elif role == "tool":
            result = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""),
                      "content": str(m.get("content") or "(no result)")}
            last = out[-1] if out else None
            if last and last["role"] == "user" and isinstance(last["content"], list) \
                    and last["content"] and last["content"][0].get("type") == "tool_result":
                last["content"].append(result)  # every result for one turn goes in one message
            else:
                out.append({"role": "user", "content": [result]})
        elif role == "assistant":
            if m.get("_claude"):
                out.append({"role": "assistant", "content": m["_claude"]})
            elif m.get("tool_calls"):
                blocks: list[dict] = [{"type": "text", "text": m["content"]}] if m.get("content") else []
                for c in m["tool_calls"]:
                    try:
                        args = json.loads(c["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    blocks.append({"type": "tool_use", "id": c.get("id", c["function"]["name"]),
                                   "name": c["function"]["name"], "input": args})
                out.append({"role": "assistant", "content": blocks})
            elif m.get("content"):
                out.append({"role": "assistant", "content": m["content"]})
        elif m.get("content"):
            out.append({"role": "user", "content": m["content"]})
    while out and out[0]["role"] == "assistant":
        out.pop(0)  # the conversation has to start with the user
    return "\n\n".join(s for s in system if s), out


def _error_text(e: anthropic.APIStatusError) -> str:
    body = e.body if isinstance(e.body, dict) else {}
    return str((body.get("error") or {}).get("message") or e.message)


class ClaudeLLM:
    def __init__(self, settings: Settings, http_client=None):
        self.settings = settings
        self._http = http_client
        self._client: anthropic.AsyncAnthropic | None = None
        self._client_key = ""
        self.blocked_until = 0.0
        self.last_error = ""
        self.last_model = ""

    def has_key(self) -> bool:
        return bool(self.settings.secret("ANTHROPIC_API_KEY"))

    def ready(self) -> bool:
        return anthropic is not None and self.has_key() and time.time() >= self.blocked_until

    def model(self, tier: str) -> str:
        return self.settings["claude_smart_model" if tier == "smart" else "claude_fast_model"]

    def _api(self) -> anthropic.AsyncAnthropic:
        if anthropic is None:
            raise LLMError(NOT_INSTALLED)
        key = self.settings.secret("ANTHROPIC_API_KEY")
        if not key:
            raise LLMError("No Claude API key set")
        if self._client is None or key != self._client_key:
            self._client = anthropic.AsyncAnthropic(
                api_key=key, max_retries=1, timeout=anthropic.Timeout(45.0, connect=5.0), http_client=self._http)
            self._client_key = key
        return self._client

    def _fail(self, e: Exception, model: str) -> LLMError:
        """Turn an API error into an LLMError, pausing Claude for a while when retrying is pointless."""
        if isinstance(e, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
            self.blocked_until = time.time() + 600
            msg = "Anthropic didn't accept the Claude key"
        elif isinstance(e, anthropic.RateLimitError):
            retry = e.response.headers.get("retry-after", "")
            self.blocked_until = time.time() + (float(retry) if retry.replace(".", "", 1).isdigit() else 20)
            msg = "Claude is rate limited"
        elif isinstance(e, anthropic.NotFoundError):
            self.blocked_until = time.time() + 600
            msg = f"Claude model {model} wasn't found; check the model name in Settings"
        elif isinstance(e, anthropic.BadRequestError) and "credit" in _error_text(e).lower():
            self.blocked_until = time.time() + 1800
            msg = "the Anthropic account has no credit left"
        elif isinstance(e, anthropic.APIStatusError):
            msg = f"Claude HTTP {e.status_code}: {_error_text(e)[:160]}"
        elif isinstance(e, anthropic.APIConnectionError):
            msg = "couldn't reach Anthropic"
        else:
            msg = str(e)
        self.last_error = msg
        log.warning("Claude (%s): %s", model, msg)
        return LLMError(msg)

    async def chat(self, messages: list[dict], *, tier: str = "fast", tools: list[dict] | None = None,
                   json_mode: bool = False, max_tokens: int = 600, temperature: float | None = None,
                   builtin_tools: list[dict] | None = None) -> ClaudeResult:
        # temperature and builtin_tools are Groq knobs; Claude only takes default sampling.
        api = self._api()
        if time.time() < self.blocked_until and self.last_error:
            raise LLMError(self.last_error)  # key rejected or out of credit: don't hammer the API
        model = self.model(tier)
        system, msgs = convert_messages(messages)
        if json_mode:
            system = f"{system}\n\n{JSON_ONLY}".strip()
        params: dict = {
            "model": model,
            # Thinking counts towards max_tokens, so leave room for it on top of the reply.
            "max_tokens": max(4096, max_tokens + 2048),
            "messages": msgs,
            "output_config": {"effort": EFFORT.get(tier, "low")},
        }
        if system:
            params["system"] = system
        if tools:
            params["tools"] = convert_tools(tools)
        t0 = time.monotonic()
        try:
            r = await api.messages.create(**params)
        except anthropic.APIError as e:
            raise self._fail(e, model) from e
        if r.stop_reason == "refusal":
            self.last_error = "Claude declined that request"
            raise LLMError(self.last_error)
        text = "".join(b.text for b in r.content if b.type == "text").strip()
        calls = [{"id": b.id, "type": "function", "function": {"name": b.name, "arguments": json.dumps(b.input)}}
                 for b in r.content if b.type == "tool_use"]
        self.last_error = ""
        self.last_model = model
        return ClaudeResult(content=text, tool_calls=calls, model=model,
                            usage=r.usage.model_dump(exclude_none=True), ms=int((time.monotonic() - t0) * 1000),
                            blocks=[b.model_dump(exclude_none=True) for b in r.content])

    async def web_search(self, query: str) -> str:
        """Claude's own web search, answered in a few spoken sentences."""
        model = self.model("smart")
        # The dynamic-filtering version runs on Sonnet and Opus; the basic one runs everywhere.
        kind = "web_search_20260209" if ("sonnet" in model or "opus" in model) else "web_search_20250305"
        msgs: list[dict] = [{"role": "user", "content": query}]
        try:
            api = self._api()
            for _ in range(3):
                r = await api.messages.create(
                    model=model, max_tokens=4096, system=SEARCH_SYSTEM, messages=msgs,
                    tools=[{"type": kind, "name": "web_search", "max_uses": 3}],
                    output_config={"effort": "low"})
                if r.stop_reason != "pause_turn":
                    break
                # A long search can pause; sending its own reply back lets it carry on.
                msgs = msgs[:1] + [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in r.content]}]
            found = "".join(b.text for b in r.content if b.type == "text").strip()
            return found or "The search didn't find anything useful."
        except (anthropic.APIError, LLMError) as e:
            log.info("Claude web search failed: %s", e)
            return "Web search isn't available right now."

    async def check(self) -> tuple[int | None, str]:
        """Try the key with a tiny request. Returns (status code or None, message for the user)."""
        if anthropic is None:
            return None, "Key saved, but " + NOT_INSTALLED + "."
        self.blocked_until = 0.0  # a new key gets a fresh chance
        model = self.model("fast")
        try:
            await self._api().messages.create(
                model=model, max_tokens=512, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "Reply with the single word: ok"}])
        except anthropic.APIStatusError as e:
            err = self._fail(e, model)
            if e.status_code in (401, 403):
                return e.status_code, "Anthropic didn't accept that key. Copy it again (it starts with sk-ant-)."
            if "no credit" in str(err):
                return 402, ("The key works, but your Anthropic account has no credit yet. Add some at "
                             "console.anthropic.com under Billing, then click Save again.")
            if e.status_code == 404:
                return 404, f"The key works, but the model {model} wasn't found. Check the Claude model in Settings."
            return e.status_code, f"Key saved, but Claude said: {_error_text(e)[:140]}"
        except anthropic.APIConnectionError:
            return None, "Key saved, but Anthropic couldn't be reached to check it. Is the internet on?"
        self.blocked_until = 0.0
        self.last_error = ""
        return 200, "Claude key works. Buddy is ready to think."

    def status(self) -> dict:
        return {
            "key": self.has_key(),
            "fast": self.model("fast"),
            "smart": self.model("smart"),
            "paused_for": max(0, round(self.blocked_until - time.time())),
            "last_error": self.last_error or (NOT_INSTALLED if anthropic is None and self.has_key() else ""),
        }
