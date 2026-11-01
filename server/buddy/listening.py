"""Is this sentence for Buddy?

While Buddy is on it transcribes everything said near the desk unit (transcribed on this computer), and
asks Groq's free model whether each sentence is meant for Buddy, given what was said just
before. So "let me ask you a question" works without a wake word, while "my buddy Dave said..."
or a chat between other people is ignored. Without a Groq key, or when Groq is busy, simple
phrase rules decide instead.
"""
from __future__ import annotations

import logging
import re
import time
from collections import deque
from datetime import datetime
from zoneinfo import ZoneInfo

from . import intents
from .config import Settings
from .llm import GroqLLM, LLMError, parse_json_loose

log = logging.getLogger("buddy.listening")

# Phrases that are clearly aimed at Buddy, used when Groq can't be asked.
DIRECT = re.compile(
    r"^\W*((hey|ok|okay|hi)\W+)?buddy\b|\blet me ask you\b|\b(a )?(quick )?question for you\b|"
    r"\bcan i ask you\b|\bare you (there|listening|awake)\b", re.I)

JUDGE = """You decide whether a sentence heard by a bedroom voice assistant called {name} was said TO it.
People nearby also talk to each other, watch TV and play music, and may say "buddy" in other ways.
Reply with JSON only: {{"for_assistant": true}} or {{"for_assistant": false}}.
True only when the speaker is clearly talking to the assistant, for example:
- they address it by name at the start ("Buddy, ...", "hey buddy ...");
- they announce a question for it ("let me ask you a question", "quick question for you");
- they ask a request or question that follows on from what the assistant just said.
False for: talk between people, mid-sentence mentions ("my buddy Dave", "Buddy Holly"), TV or
radio, songs, thinking out loud, and anything unclear. When unsure, say false."""


class ContextJudge:
    def __init__(self, settings: Settings, groq: GroqLLM):
        self.settings = settings
        self.groq = groq
        self.heard: deque = deque(maxlen=8)  # (time, text) said near Buddy, oldest first
        self.last_how = ""

    def hear(self, text: str) -> None:
        self.heard.append((time.time(), text))

    def recent(self, now: float, window_s: float = 90.0) -> list[str]:
        return [t for ts, t in self.heard if now - ts < window_s]

    async def is_for_buddy(self, text: str, mentioned: bool = False, last_reply: str = "",
                           since_reply_s: float = 1e9) -> bool:
        """mentioned: the wake word fired somewhere in this sentence."""
        t = text.strip()
        if not t:
            return False
        if intents.has_wake(t):
            return self._verdict(True, "starts with hey buddy")
        if since_reply_s < 60 and intents.is_off(intents.strip_wake(t)):
            return self._verdict(True, "told Buddy to go off soon after it spoke")
        if since_reply_s < 8 and intents.match(intents.strip_wake(t), _now(self.settings)) is not None:
            return self._verdict(True, "a quick command right after Buddy spoke")
        if self.settings.secret("GROQ_API_KEY"):
            try:
                return self._verdict(await self._ask_groq(t, mentioned, last_reply, since_reply_s), "Groq")
            except LLMError as e:
                log.info("context check via Groq failed (%s); using phrase rules", e)
        return self._verdict(bool(DIRECT.search(t)), "phrase rules")

    async def _ask_groq(self, text: str, mentioned: bool, last_reply: str, since_reply_s: float) -> bool:
        now = time.time()
        lines = [f"- {t}" for t in self.recent(now)[:-1]]  # the newest is the sentence itself
        parts = []
        if lines:
            parts.append("Said nearby in the last minute or so (oldest first):\n" + "\n".join(lines[-6:]))
        if last_reply and since_reply_s < 120:
            parts.append(f'The assistant last said, {int(since_reply_s)} seconds ago: "{last_reply[:200]}"')
        if mentioned:
            parts.append("(The wake-word detector heard something like \"buddy\" in this sentence.)")
        parts.append(f'New sentence: "{text}"')
        messages = [{"role": "system", "content": JUDGE.format(name=self.settings["assistant_name"])},
                    {"role": "user", "content": "\n\n".join(parts)}]
        # Room for the model's own (hidden) reasoning, which counts towards the limit, plus the answer.
        res = await self.groq.chat(messages, tier="fast", json_mode=True, max_tokens=400, temperature=0)
        data = parse_json_loose(res.content)
        if isinstance(data, dict) and "for_assistant" in data:
            return bool(data["for_assistant"])
        return "true" in (res.content or "").lower()

    def _verdict(self, value: bool, how: str) -> bool:
        self.last_how = how
        return value


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings["timezone"]))

