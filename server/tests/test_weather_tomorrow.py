"""Bug: "Can you tell me the weather to morrow?" (speech-to-text split the word) got today's weather, then
"I understand the question, but right now..." and an apology. Tomorrow should come straight back."""
import asyncio
import time

from buddy import intents
from buddy.brain import MISMATCH, Brain, Conversation
from tests.fakes import claude_message, claude_text
from tests.test_llm_brain import make_brain


def test_split_and_misspelt_tomorrow_are_put_back():
    for heard in ("the weather to morrow", "the weather to-morrow", "weather tomorow", "weather 2moro",
                  "weather tmrw", "weather tomorrow", "weather tomor row"):
        assert "tomorrow" in intents.fix_heard(heard), heard
    assert intents.fix_heard("what about to nite") == "what about tonight"
    assert intents.fix_heard("I'm going to Morocco") == "I'm going to Morocco"
    assert intents.fix_heard("I want to more often") == "I want to more often"


def test_to_morrow_skips_todays_ready_answer(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    brain.weather_ready = (time.time(), "TODAY'S READY ANSWER")
    r = asyncio.run(brain.handle("Can you tell me the weather to morrow?", Conversation()))
    assert r.intent == "weather" and "TODAY'S READY ANSWER" not in r.text
    assert "tomorrow" in r.text.lower()


def test_reword_catches_an_answer_to_the_wrong_question(settings, apis, claude):
    settings.update({"reword_replies": True})
    brain = make_brain(settings, apis, claude)
    brain.weather_ready = (time.time(), "Right now it's 8 degrees and cloudy.")
    claude.script = [claude_message(claude_text(MISMATCH)),  # the reword spots it doesn't fit
                     claude_message(claude_text("Tomorrow looks dry, around 9 degrees."))]
    # "this morrow" isn't a word Buddy can repair, so the quick path answers today: the check must catch it.
    r = asyncio.run(brain.handle("what's the weather like for this morrow then", Conversation()))
    assert r.text == "Tomorrow looks dry, around 9 degrees."
    assert "understand" not in r.text.lower() and "sorry" not in r.text.lower()
