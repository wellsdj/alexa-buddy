"""Saving notes, and cooking mode."""
import asyncio

from buddy.brain import Conversation
from tests.fakes import claude_message, claude_text, claude_tool
from tests.test_llm_brain import make_brain


def test_save_and_find_a_note(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_message(claude_tool("notes", {"action": "save", "title": "Bruschetta ingredients",
                                                          "text": "red onion, tomatoes, olive oil, garlic, basil, ciabatta"}))]
    r = asyncio.run(brain.handle("save those ingredients", Conversation()))
    assert r.text == "Saved: Bruschetta ingredients."
    claude.script = [claude_message(claude_tool("notes", {"action": "find", "query": "bruschetta"})),
                     claude_message(claude_text("Red onion, tomatoes, olive oil, garlic, basil and ciabatta."))]
    asyncio.run(brain.handle("what were the bruschetta ingredients", Conversation()))
    assert "ciabatta" in claude.requests[-1]["messages"][-1]["content"][0]["content"]


def test_cooking_mode_guides_and_keeps_listening(settings, apis, claude):
    import json
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    claude.script = [claude_message(claude_tool("cooking", {"action": "start", "dish": "pasta"})),
                     claude_message(claude_text("Lovely. How many people are you cooking for?"))]
    r = asyncio.run(brain.handle("I'm going to cook pasta, help me", conv))
    assert conv.cooking == "pasta" and r.follow_up
    claude.script = [claude_message(claude_text("Boil the water first."))]
    r = asyncio.run(brain.handle("two people", conv))
    assert r.follow_up and "Cooking mode is on" in json.dumps(claude.requests[-1]["system"])
