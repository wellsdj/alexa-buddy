"""Picks Buddy's brain: Claude when its key is set (or chosen in Settings), Groq otherwise.

Groq stays the safety net: if Claude can't answer (no credit, key rejected, outage), the same
request goes to Groq so Buddy never goes quiet. Groq also keeps doing speech-to-text (stt.py).
"""
from __future__ import annotations

import logging

from .claude import ClaudeLLM
from .config import Settings
from .llm import ChatResult, GroqLLM, LLMError

log = logging.getLogger("buddy.router")


def for_groq(messages: list[dict]) -> list[dict]:
    """Groq rejects fields it doesn't know, so drop Claude's private copy of its reply blocks."""
    return [{k: v for k, v in m.items() if k != "_claude"} if "_claude" in m else m for m in messages]


class LLMRouter:
    def __init__(self, settings: Settings, groq: GroqLLM, claude: ClaudeLLM):
        self.settings = settings
        self.groq = groq
        self.claude = claude

    def use_claude(self) -> bool:
        return self.settings["brain"] != "groq" and self.claude.ready()

    async def chat(self, messages: list[dict], **kw) -> ChatResult:
        if self.use_claude():
            try:
                return await self.claude.chat(messages, **kw)
            except LLMError as e:
                if not self.settings.secret("GROQ_API_KEY"):
                    raise
                log.info("Claude couldn't answer (%s); asking Groq instead", e)
        return await self.groq.chat(for_groq(messages), **kw)

    async def web_search(self, query: str) -> str:
        if self.use_claude():
            found = await self.claude.web_search(query)
            if found:
                return found
        return await self.groq.web_search(query)

    async def discover(self, force: bool = False):
        return await self.groq.discover(force)

    def status(self) -> dict:
        st = self.groq.status()
        st["claude"] = self.claude.status()
        st["brain"] = "claude" if self.use_claude() else "groq"
        if st["brain"] == "claude":
            st["fast"], st["smart"] = [self.claude.model("fast")], [self.claude.model("smart")]
        elif self.claude.has_key() and self.settings["brain"] != "groq" and st["claude"]["last_error"]:
            st["last_error"] = f"Claude: {st['claude']['last_error']}" + (
                f"; {st['last_error']}" if st.get("last_error") else "")
        return st
