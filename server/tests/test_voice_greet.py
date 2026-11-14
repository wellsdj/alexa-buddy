"""Greeting people by voice, and asking a new voice "who's this?" (then remembering them)."""
import asyncio

import numpy as np
import pytest

from buddy.intents import name_answer
from buddy.voiceid import VoiceID
from tests.test_quiet_wake import setup
from tests.test_voiceid import fake_embed, voice


@pytest.mark.parametrize("text,name", [
    ("It's Sam.", "Sam"), ("I'm Priya", "Priya"), ("this is Mr Jones", "Mr Jones"), ("Oh hi, Ravi here", "Ravi"),
    ("Hey Buddy, my name is Lucy", "Lucy"), ("Tom", "Tom"),
    ("what's the time", None), ("never mind", None), ("nobody", None), ("can you set a timer", None),
    ("it doesn't matter", None), ("stop", None),
])
def test_names_in_answers(text, name):
    assert name_answer(text) == name


def turn(hub, s, heard, audio):
    hub.stt.apis.transcripts.append(heard)
    asyncio.run(hub.process_audio(s, audio, len(audio) / 16000))


@pytest.fixture
def known(settings, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    monkeypatch.setattr(VoiceID, "embed", fake_embed)
    hub, s, ws, spoken = setup(settings, "")
    hub.stt.apis.transcripts.clear()
    hub.voiceid.add_audio("Wells", voice(200, 70))  # over the 60 s it needs to be sure
    return hub, s, spoken


def test_greets_someone_it_knows_once(known):
    hub, s, spoken = known
    turn(hub, s, "what time is it", voice(200, 3, seed=4))
    assert spoken[-1].split()[:2] in (["Hey", "Wells!"], ["Hi", "Wells!"], ["Alright", "Wells?"])
    turn(hub, s, "what time is it", voice(200, 3, seed=5))
    assert "Wells" not in spoken[-1]  # not every time


def test_asks_a_new_voice_who_it_is_then_remembers_them(known):
    hub, s, spoken = known
    turn(hub, s, "what time is it", voice(700, 3, seed=6))
    assert spoken[-1] == "Oh, hello! Who's this?"
    turn(hub, s, "It's Sam", voice(700, 3, seed=7))
    assert spoken[-1].startswith("Nice to meet you, Sam!") and ":" in spoken[-1]  # then the time it held back
    assert "Sam" in hub.voiceid.people
    r = hub.voiceid.identify(voice(700, 3, seed=8))
    assert r["name"] == "" and r["learning"] == "Sam"  # not named until it's heard 60 s of Sam
    before = hub.voiceid.people["Sam"]["seconds"]
    turn(hub, s, "what time is it", voice(700, 3, seed=9))
    assert spoken[-1] != "Oh, hello! Who's this?"  # doesn't ask again
    assert hub.voiceid.people["Sam"]["seconds"] > before  # learning from every sentence


def test_not_a_name_carries_on_as_a_normal_request(known):
    hub, s, spoken = known
    turn(hub, s, "what time is it", voice(700, 3, seed=6))
    turn(hub, s, "what time is it", voice(700, 3, seed=9))
    assert spoken[-1] != "Oh, hello! Who's this?" and "Sam" not in hub.voiceid.people


def test_saying_your_own_name_teaches_it_your_voice_better(known):
    hub, s, spoken = known
    hub.settings.update({"voice_id_threshold": 1.01})  # pretend it didn't recognise Wells this time
    turn(hub, s, "what time is it", voice(200, 3, seed=10))
    assert spoken[-1] == "Oh, hello! Who's this?"
    turn(hub, s, "it's wells", voice(200, 3, seed=11))
    assert spoken[-1].startswith("Oh, sorry Wells, I didn't recognise you!")
    assert list(hub.voiceid.people) == ["Wells"]


def test_switched_off_means_no_greetings(known):
    hub, s, spoken = known
    hub.settings.update({"greet_by_voice": False})
    turn(hub, s, "what time is it", voice(700, 3, seed=6))
    assert "Who's this" not in spoken[-1]


def test_who_said_what_goes_to_claude(known, monkeypatch):
    hub, s, spoken = known
    seen = []

    async def fake_llm(text, conv, ctx, **kw):
        seen.append(ctx)
        from buddy.brain import Reply
        return Reply("Sure.", intent="llm")

    monkeypatch.setattr(hub.brain, "_llm", fake_llm)
    turn(hub, s, "tell me a joke", voice(200, 3, seed=12))
    assert seen[-1]["speaker"] == "Wells" and "Wells" in seen[-1]["present"]
    assert hub.conv.history[0]["speaker"] == "Wells"
    assert hub.conv.transcript[0]["content"] == "[Wells] tell me a joke"
