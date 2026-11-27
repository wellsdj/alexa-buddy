"""School jobs only read, so they get a much shorter rule book (sent, cached, on every step)."""
import asyncio

import pytest

from buddy.online import SCHOOL_SYSTEM, SYSTEM, Browser, Job, OnlineAgent


class Stop(Exception):
    pass


class Peek:
    async def chat(self, messages, **kw):
        self.messages = list(messages)
        raise Stop


def first_system(tmp_path, kind):
    llm = Peek()
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm,
                        Browser(tmp_path / "profile", visible=False))
    with pytest.raises(Stop):
        asyncio.run(agent._loop(Job(task="x", kind=kind), "Monday", "Richmond"))
    return llm.messages[0]


def test_school_jobs_get_the_short_rule_book(tmp_path):
    m = first_system(tmp_path, "school_homework")
    assert m["cache"] is True and "READ something from the user's school sites" in m["content"]
    assert "Card or bank details" not in m["content"] and "ClubSpark" not in m["content"]
    assert len(SCHOOL_SYSTEM) < len(SYSTEM) / 2


def test_other_jobs_keep_the_full_rule_book(tmp_path):
    m = first_system(tmp_path, "tennis")
    assert "Card or bank details" in m["content"]
