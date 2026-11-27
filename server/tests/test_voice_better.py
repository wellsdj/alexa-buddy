"""Voice ID, better: best-few-samples matching, safe learning, scores logged, names checked before saving,
your own name recognised by how you say it, and "No, I'm Wells" putting it right."""
import asyncio
import json

import numpy as np
import pytest

from buddy.intents import correction_name, name_answer
from buddy.voiceid import VoiceID, utterances
from tests.test_quiet_wake import setup
from tests.test_voice_greet import turn
from tests.test_voiceid import fake_embed, voice


@pytest.fixture
def known(settings, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    monkeypatch.setattr(VoiceID, "embed", fake_embed)
    hub, s, ws, spoken = setup(settings, "")
    hub.stt.apis.transcripts.clear()
    hub.voiceid.add_audio("Wells", voice(200, 70))
    return hub, s, spoken


def test_matches_on_the_closest_few_samples_too(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    monkeypatch.setattr(VoiceID, "embed", fake_embed)
    vid = VoiceID(tmp_path)
    vid.add_audio("Wells", voice(200, 40))
    vid.add_audio("Wells", voice(320, 8, seed=3))  # the same person on another mic, a bit different
    v = fake_embed(None, voice(320, 3, seed=9))
    centre_only = float(v @ vid._centroids["Wells"])
    assert vid.score(v, "Wells") > centre_only + 0.05  # the close samples count, not just the average


@pytest.mark.parametrize("text,name", [
    ("say about", None), ("I'm hungry", None), ("about that", None), ("Sam", "Sam")])
def test_everyday_words_are_not_names(text, name):
    assert name_answer(text) == name


@pytest.mark.parametrize("text,name", [
    ("No, I'm Wells", "Wells"), ("no it's wells", "Wells"), ("It's Wells, not Sam", "Wells"),
    ("that's not me, it's Wells", "Wells"), ("I'm tired", None), ("no I'm hungry", None),
    ("I'm Wells", None)])
def test_corrections(text, name):
    assert correction_name(text) == name


def test_a_wrong_name_is_checked_and_can_be_put_right(known):
    hub, s, spoken = known
    turn(hub, s, "what time is it", voice(700, 3, seed=6))
    turn(hub, s, "say about", voice(700, 3, seed=7))  # mis-heard: not taken as a name
    assert "Say About" not in hub.voiceid.people
    hub.conv.history, hub.conv.last_active = [], 0  # later on: a new conversation
    turn(hub, s, "what time is it", voice(700, 3, seed=8))
    assert spoken[-1] == "Oh, hello! Who's this?"
    turn(hub, s, "It's Pam", voice(700, 3, seed=9))
    assert spoken[-1] == "Nice to meet you! Did I get that right, Pam?"
    turn(hub, s, "no", voice(700, 3, seed=10))
    assert spoken[-1] == "Sorry! What's your name?"
    turn(hub, s, "Sam", voice(700, 3, seed=11))
    assert spoken[-1] == "Nice to meet you! Did I get that right, Sam?"
    turn(hub, s, "no, it's Sam with an S", voice(700, 3, seed=12))
    turn(hub, s, "yes that's right", voice(700, 3, seed=13))
    assert "Pam" not in hub.voiceid.people


def test_your_name_by_how_you_say_it(known):
    hub, s, spoken = known
    names = np.concatenate([np.concatenate([voice(450, 0.6, seed=i, gaps=False),
                                            np.zeros(int(0.5 * 16000), np.int16)]) for i in range(20)])
    assert len(utterances(names)) == 20
    assert hub.voiceid.add_name_audio("Wells", names)["name_samples"] == 20
    hub.settings.update({"voice_id_threshold": 1.01})  # it doesn't recognise the voice this time
    turn(hub, s, "what time is it", voice(200, 3, seed=10))
    assert spoken[-1] == "Oh, hello! Who's this?"
    turn(hub, s, "it's wales", voice(450, 0.8, seed=30, gaps=False))  # the ears heard "Wales"
    assert spoken[-1].startswith("Oh, sorry Wells, I didn't recognise you!")
    assert list(hub.voiceid.people) == ["Wells"]


def test_no_im_wells_moves_the_voice_to_the_right_person(known):
    hub, s, spoken = known
    hub.voiceid.add_audio("Sam", voice(700, 70, seed=2))
    hub.settings.update({"voice_id_threshold": 0.0})
    turn(hub, s, "what time is it", voice(700, 3, seed=40))  # taken for Sam
    assert hub.last_who["name"] == "Sam"
    sam_before = len(hub.voiceid.people["Sam"]["samples"])
    turn(hub, s, "No, I'm Wells", voice(700, 3, seed=41))
    assert spoken[-1] == "Sorry Wells, my mistake! I'll remember that's your voice."
    assert len(hub.voiceid.people["Sam"]["samples"]) <= sam_before  # what it just learnt for Sam is taken back
    assert "corrected by you" in hub.voiceid.people["Wells"]["sources"]


def test_match_scores_are_logged(known):
    hub, s, spoken = known
    turn(hub, s, "what time is it", voice(200, 3, seed=4))
    rows = [json.loads(x) for x in (hub.settings.data_dir / "voice_scores.jsonl").read_text().splitlines()]
    assert rows[-1]["best"] == "Wells" and rows[-1]["score"] > 0.5 and "margin" in rows[-1]


def test_learning_skips_muddled_sentences(settings):
    from buddy.hub import Hub
    hub = Hub(settings)
    added = []
    hub.voiceid.add_print = lambda name, v, s: added.append(name)
    hub.voiceid.trusted = lambda name, t: True
    v = np.zeros(4)
    asyncio.run(hub._learn_voice({"name": "Wells", "score": 0.6, "best": "Wells", "print": v, "seconds": 3,
                                  "margin": 0.02}))  # nearly as close to someone else
    asyncio.run(hub._learn_voice({"name": "Wells", "score": 0.6, "best": "Wells", "print": v, "seconds": 3,
                                  "speech_s": 0.8, "margin": 0.4}))  # hardly any talking in it
    assert added == []
    for _ in range(40):
        asyncio.run(hub._learn_voice({"name": "Wells", "score": 0.6, "best": "Wells", "print": v, "seconds": 3,
                                      "speech_s": 2.5, "margin": 0.4}))
    assert len(added) == 30  # a day's worth, at most
