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
from tests.fakes import chat_response, claude_message, claude_text
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
    app = create_app(settings, apis.client(), None, apis.stt())
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
    app = create_app(settings, apis.client(), None, apis.stt())
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
    app = create_app(settings, apis.client(), None, apis.stt())
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


# ---------- listening to the room, context and "off" ----------
def judge(verdict: bool):
    return chat_response("openai/gpt-oss-20b", json.dumps({"for_assistant": verdict}))


def said(clip_name: str, rng, before: float = 1.0, after: float = 2.0) -> np.ndarray:
    return np.concatenate([room(before, rng), (clip(clip_name) * 0.6).astype(np.int16), room(after, rng)])


def wait_for_log(hub, event: str, timeout: float = 10.0) -> dict:
    """Room listening happens in the background; wait until it has decided."""
    import time
    end = time.time() + timeout
    while time.time() < end:
        found = [e for e in hub.log if e["event"] == event]
        if found:
            return found[-1]
        time.sleep(0.05)
    raise AssertionError(f"no {event!r} in the log: {[e['event'] for e in hub.log]}")


def wake_earcons(seen) -> int:
    return sum(1 for m in seen if m.get("type") == "earcon" and m.get("name") == "wake")


def test_question_without_wake_word_is_answered_in_context(settings, apis, claude):
    rng = np.random.default_rng(3)
    apis.transcripts = ["Let me ask you a question."]
    apis.chat_script = [judge(True)]
    claude.script = [claude_message(claude_text("Of course. What is it?"))]
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, said("room_let_me_ask__en-gb-alan-low__0.9.wav", rng))
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"] == "Of course. What is it?" and wake_earcons(seen) == 1
        asked = apis.chat_requests[0]["messages"][-1]["content"]
        assert 'New sentence: "Let me ask you a question."' in asked
        heard = [e for e in app.state.hub.log if e["event"] == "heard"]
        assert heard[-1]["for_buddy"] and heard[-1]["how"] == "Groq"


def test_people_talking_to_each_other_is_ignored(settings, apis, claude):
    rng = np.random.default_rng(4)
    apis.transcripts = ["What do you fancy for dinner tonight?"]
    apis.chat_script = [judge(False)]
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, said("room_dinner__en-us-lessac-medium__0.9.wav", rng))
        heard = wait_for_log(app.state.hub, "heard")
        assert heard["heard"].startswith("What do you fancy") and not heard["for_buddy"]
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))  # a marker to stop reading at
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("It's ") and wake_earcons(seen) == 0
        assert not claude.requests


@pytest.mark.parametrize("name", [f"{phrase}__{v}__{speed}.wav" for phrase in ("mid_my_buddy", "mid_hey_buddy")
                                  for v in ("en-gb-alan-low", "en-us-lessac-medium") for speed in ("0.9", "1.15")])
def test_buddy_mid_sentence_does_not_wake(settings, apis, claude, name):
    rng = np.random.default_rng(5)
    apis.transcripts = ["So yesterday I told my buddy about the match." if "my_buddy" in name
                        else "So I walked in and went hey buddy to my mate, and he just ignored me."]
    apis.chat_script = [judge(False)]
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, said(name, rng))
        heard = wait_for_log(app.state.hub, "heard")
        assert heard["heard"].startswith("So ") and not heard["for_buddy"]
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))
        start, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("It's ")
        assert wake_earcons(seen) == 0 and not any(m.get("state") == "listening" for m in seen)
        assert not claude.requests


def test_off_stops_room_listening_until_hey_buddy(settings, apis, claude):
    rng = np.random.default_rng(6)
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    hub = app.state.hub
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        # 1) "Hey buddy, off" -> Buddy says so and goes to sleep.
        apis.transcripts = ["Hey buddy, off."]
        stream(ws, said("pos_hey_buddy_off__en-gb-alan-low__0.9.wav", rng))
        start, _ = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("Okay, I'm off") and not hub.awake
        recv_until(ws, lambda m: m.get("type") == "audio_end")
        ws.send_text(json.dumps({"type": "playback_done", "seq": start["seq"]}))
        recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "idle")
        # 2) While off, talk in the room isn't even transcribed.
        heard_before = len(apis.stt_requests)
        stream(ws, said("room_let_me_ask__en-gb-alan-low__0.9.wav", rng))
        import time
        time.sleep(1.5)  # give it every chance to (wrongly) pick the sentence up
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))
        recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert len(apis.stt_requests) == heard_before and not hub.awake
        # 3) "Hey buddy" out of nowhere turns it back on.
        apis.transcripts = ["Hey buddy."]
        stream(ws, said("pos_hey_buddy__en-us-lessac-medium__0.9.wav", rng, before=1.5))
        _, seen = recv_until(ws, lambda m: m.get("type") == "earcon" and m.get("name") == "wake")
        assert hub.awake


def test_mid_sentence_hey_buddy_with_room_listening_off_is_judged_in_context(settings, apis, claude):
    settings.update({"room_listening": False})
    rng = np.random.default_rng(7)
    apis.transcripts = ["So I walked in and went hey buddy to my mate, and he just ignored me."]
    apis.chat_script = [judge(False)]
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, said("mid_hey_buddy__en-us-lessac-medium__0.9.wav", rng))
        heard = wait_for_log(app.state.hub, "heard")
        assert not heard["for_buddy"]
        assert "wake-word detector heard" in apis.chat_requests[-1]["messages"][-1]["content"]
        ws.send_text(json.dumps({"type": "text", "text": "what time is it"}))
        _, seen = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert wake_earcons(seen) == 0 and not claude.requests


def test_hey_buddy_then_a_pause_waits_for_the_question(settings, apis, claude):
    rng = np.random.default_rng(8)
    apis.transcripts = ["Hey buddy.", "What time is it?"]
    app = create_app(settings, apis.client(), claude.client(), apis.stt())
    with TestClient(app) as client, device(client) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        recv_until(ws, lambda m: m.get("type") == "alarms")
        stream(ws, said("pos_hey_buddy__en-us-lessac-medium__0.9.wav", rng, after=1.5))
        recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "thinking")
        recv_until(ws, lambda m: m.get("type") == "state" and m.get("state") == "listening")  # listening again
        stream(ws, said("room_dinner__en-us-lessac-medium__0.9.wav", rng, before=0.3))  # any speech will do
        start, _ = recv_until(ws, lambda m: m.get("type") == "audio_start")
        assert start["text"].startswith("It's ")
