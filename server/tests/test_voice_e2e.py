"""Streams real speech into the device socket exactly like the ESP32 does (20 ms frames).

Needs spoken test clips, which aren't committed (the TTS voices used have their own
licences). Generate them with tests/make_clips.py, then:
    BUDDY_TEST_CLIPS=/path/to/clips pytest tests/test_voice_e2e.py
"""
import json
import os
import wave
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from buddy.main import create_app
from tests.test_server import recv_until

CLIPS = Path(os.environ.get("BUDDY_TEST_CLIPS", "/nonexistent"))
pytestmark = pytest.mark.skipif(not CLIPS.exists(), reason="set BUDDY_TEST_CLIPS to a folder of test clips")


def clip(name: str) -> np.ndarray:
    with wave.open(str(CLIPS / name)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def room(seconds: float, rng) -> np.ndarray:
    return rng.normal(0, 60, int(seconds * 16000)).astype(np.int16)


def stream(ws, pcm: np.ndarray) -> None:
    for i in range(0, len(pcm), 320):
        ws.send_bytes(pcm[i:i + 320].tobytes())


def device(client):
    ws = client.websocket_connect("/ws?role=device&name=esp32")
    return ws


def test_hey_buddy_then_question_is_answered(settings, apis):
    rng = np.random.default_rng(0)
    apis.transcripts = ["Hey buddy, what's the weather like?"]
    app = create_app(settings, apis.client())
    audio = np.concatenate([room(1, rng), (clip("pos_hey_buddy_q__en-us-lessac-medium__0.9.wav") * 0.6).astype(np.int16),
                            room(2, rng)])
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, audio)
        _, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        kinds = [(m.get("type"), m.get("name") or m.get("state")) for m in seen if "type" in m]
        assert ("earcon", "wake") in kinds and ("state", "listening") in kinds
        assert ("earcon", "end") in kinds and ("state", "thinking") in kinds
        start = [m for m in seen if m.get("type") == "audio_start"][0]
        assert start["text"].startswith("Right now in Richmond")
        assert apis.stt_requests and apis.stt_requests[0]["has_wav"]


def test_similar_phrase_does_not_wake(settings, apis):
    rng = np.random.default_rng(1)
    app = create_app(settings, apis.client())
    apis.transcripts = ["Hey Bobby, pass me the salt."]  # only used if a borderline check happens
    audio = np.concatenate([room(1, rng), (clip("neg_hey_bobby__en-us-lessac-medium__0.9.wav") * 0.6).astype(np.int16),
                            room(2, rng)])
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, audio)
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))  # a marker to stop reading at
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("It's ")
        assert not any(m.get("type") == "earcon" and m.get("name") == "wake" for m in seen)


def test_borderline_wake_is_confirmed_by_transcript(settings, apis):
    rng = np.random.default_rng(2)
    apis.transcripts = ["Hey buddy, set an alarm for seven."]
    app = create_app(settings, apis.client())
    audio = np.concatenate([room(1, rng), (clip("pos_hey_buddy_alarm__en-gb-alan-low__0.9.wav") * 0.6).astype(np.int16),
                            room(2, rng)])
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, audio)
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("Alarm set for 7")
        assert len(app.state.hub.alarms.list()) == 1
        log = [e["event"] for e in app.state.hub.log]
        assert "wake_verified" in log or any(m.get("name") == "wake" for m in seen)
