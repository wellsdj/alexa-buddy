"""Is this sentence for Buddy?

While Buddy is on it transcribes everything said near the desk unit (transcribed on this computer), and
uses simple phrase rules to decide whether each sentence is meant for Buddy. So "let me ask you a
question" works without a wake word, while "my buddy Dave said..." or a chat between other people
is ignored.
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

log = logging.getLogger("buddy.listening")

# Phrases that are clearly aimed at Buddy.
DIRECT = re.compile(
    r"^\W*((hey|ok|okay|hi)\W+)?buddy\b|\blet me ask you\b|\b(a )?(quick )?question for you\b|"
    r"\bcan i ask you\b|\bare you (there|listening|awake)\b", re.I)


class ContextJudge:
    def __init__(self, settings: Settings):
        self.settings = settings
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
        if intents.has_wake(t) or (mentioned and intents.wake_heard(t)):
            # "Hey buddy" heard (even with the radio or someone talking just before): it's for Buddy.
            return self._verdict(True, "hey buddy")
        if since_reply_s < 60 and intents.is_off(intents.strip_wake(t)):
            return self._verdict(True, "told Buddy to go off soon after it spoke")
        if since_reply_s < 8 and intents.match(intents.strip_wake(t), _now(self.settings)) is not None:
            return self._verdict(True, "a quick command right after Buddy spoke")
        return self._verdict(bool(DIRECT.search(t)), "phrase rules")

    def _verdict(self, value: bool, how: str) -> bool:
        self.last_how = how
        return value


def _now(settings: Settings) -> datetime:
    return datetime.now(ZoneInfo(settings["timezone"]))

