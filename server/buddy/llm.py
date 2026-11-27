"""Shared pieces for talking to a model: the error type, a chat result, and a forgiving JSON reader.

(Buddy used to call Groq from here too; Claude does all of it now, see claude.py.)
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any


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
