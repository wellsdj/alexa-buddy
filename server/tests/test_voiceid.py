"""Voice recognition logic, with a stand-in for the speaker model (the real one is a 40 MB download)."""
import numpy as np
import pytest

from buddy.voiceid import SR, VoiceID, VoiceIDError, pieces

DIM = 64


def voice(pitch: float, seconds: float, seed: int = 0, gaps: bool = True) -> np.ndarray:
    """A pretend voice: a tone at its own pitch, with pauses like speech."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * SR)) / SR
    x = 6000 * np.sin(2 * np.pi * pitch * t) + rng.normal(0, 200, len(t))
    if gaps:  # 0.25 s pause every 1.25 s
        x[(t % 1.25) > 1.0] = rng.normal(0, 20, int(np.sum((t % 1.25) > 1.0)))
    return x.astype(np.int16)


def fake_embed(self, pcm):
    """The 'voiceprint' is just the strongest pitch, spread over a few slots: same voice, same print."""
    spec = np.abs(np.fft.rfft(pcm.astype(np.float64)))
    freq = np.argmax(spec[1:]) * SR / len(pcm)
    v = np.exp(-0.5 * ((np.arange(DIM) - freq / 25.0) / 1.5) ** 2) + 0.01
    return (v / np.linalg.norm(v)).astype(np.float32)


@pytest.fixture
def vid(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    monkeypatch.setattr(VoiceID, "embed", fake_embed)
    return VoiceID(tmp_path)


def test_long_recording_is_cut_into_speech_pieces():
    x = voice(200, 30)
    parts = pieces(x)
    assert 6 <= len(parts) <= 8
    quiet = (np.random.default_rng(1).normal(0, 20, 10 * SR)).astype(np.int16)
    assert pieces(quiet) == []  # silence teaches nothing


def test_knows_who_is_talking(vid):
    vid.add_audio("Sam", voice(200, 20))
    vid.add_audio("Alex", voice(600, 20, seed=2))
    assert vid.identify(voice(200, 3, seed=5))["name"] == "Sam"
    assert vid.identify(voice(600, 3, seed=6))["name"] == "Alex"
    stranger = vid.identify(voice(1100, 3, seed=7))
    assert stranger["name"] == "" and stranger["score"] < 0.35


def test_too_short_or_quiet_isnt_guessed(vid):
    vid.add_audio("Sam", voice(200, 20))
    assert vid.identify(voice(200, 0.5))["name"] == ""  # "off": too short to tell
    quiet = (np.random.default_rng(3).normal(0, 20, 3 * SR)).astype(np.int16)
    assert vid.identify(quiet)["name"] == ""


def test_needs_a_name_and_some_speech(vid):
    with pytest.raises(VoiceIDError):
        vid.add_audio("  ", voice(200, 10))
    with pytest.raises(VoiceIDError):
        vid.add_audio("Sam", voice(200, 1.0))


def test_saved_between_restarts_and_can_be_deleted(vid, tmp_path):
    r = vid.add_audio("Sam", voice(200, 12))
    assert r["pieces"] >= 2 and r["match"] > 0.9
    again = VoiceID(tmp_path)
    assert [p["name"] for p in again.status()["people"]] == ["Sam"]
    assert again.identify(voice(200, 3, seed=9))["name"] == "Sam"
    assert again.remove("Sam") and not again.remove("Sam")
    assert VoiceID(tmp_path).status()["people"] == []


def test_topping_up_says_whether_it_sounds_like_the_same_person(vid):
    vid.add_audio("Sam", voice(200, 12))
    same = vid.add_audio("Sam", voice(200, 4, seed=4), source="desk unit mic")
    other = vid.add_audio("Sam", voice(900, 4, seed=8), source="desk unit mic")
    assert same["like_before"] > 0.9
    assert other["like_before"] < 0.35
    assert vid.status()["people"][0]["sources"] == ["recording", "desk unit mic"]


def test_nothing_saved_means_nobody_recognised(tmp_path, monkeypatch):
    monkeypatch.setattr(VoiceID, "installed", lambda self: True)
    calls = []
    monkeypatch.setattr(VoiceID, "embed", lambda self, pcm: calls.append(1))
    assert VoiceID(tmp_path).identify(voice(200, 3)) == {"name": "", "score": 0.0, "best": ""}
    assert calls == []  # and it doesn't even run the model
