"""A stray word Buddy wasn't meant to hear: say so once, then leave them to it, and stop listening."""
import asyncio

from buddy.brain import Conversation
from tests.test_llm_brain import make_brain


def test_stray_words(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    conv.last_reply = "It's sunny today."
    r = asyncio.run(brain.handle("um", conv))
    assert r.text.startswith("I'm here if you need me") and not r.follow_up
    r = asyncio.run(brain.handle("okay", conv))
    assert "leave you to it" in r.text and not r.follow_up


def test_yes_after_a_question_isnt_stray(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    conv.last_reply = "Want me to set it?"
    assert brain._stray("yeah", conv) is None


def test_saying_leave_you_to_it_stops_listening(settings, apis, claude):
    from tests.fakes import claude_message, claude_text
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_message(claude_text("No worries, I'll leave you to it."))]
    r = asyncio.run(brain.handle("that's all thanks for now", Conversation()))
    assert not r.follow_up
