"""Alarms: "stop"/"snooze" without "hey buddy" while ringing, and pausing a conversation the alarm
breaks into, then picking it back up."""
import asyncio
import json

import numpy as np

from buddy.hub import Hub, Session
from tests.fakes import FakeAPIs, FakeSTT


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    async def send_bytes(self, data):
        pass

    def types(self):
        return [m["type"] for m in self.sent]


def setup(settings, heard=""):
    apis = FakeAPIs()
    apis.transcripts = [heard] if heard else []
    stt = FakeSTT(apis)
    hub = Hub(settings, stt=stt)
    ws = FakeWS()
    s = Session(hub, ws, "device", "esp32")
    hub.sessions = {s.id: s}
    spoken = []

    async def speak(text, then_listen=False):
        spoken.append((text, then_listen))

    s.speak = speak
    return hub, s, ws, spoken, stt


def ring(hub, s):
    a = hub.alarms.add_alarm("07:00")
    asyncio.run(hub.alarm_fired(s, a.id, "alarm"))
    return a


def test_stop_while_ringing_needs_no_wake_word(settings):
    hub, s, ws, spoken, _ = setup(settings, "Stop!")
    ring(hub, s)
    asyncio.run(hub.ring_heard(s, np.zeros(16000, np.int16)))
    assert ws.sent[-1]["type"] == "alarm_stop"
    assert s.ringing is None


def test_snooze_while_ringing_snoozes_for_the_minutes_said(settings):
    hub, s, ws, spoken, _ = setup(settings, "five more minutes")
    a = ring(hub, s)
    asyncio.run(hub.ring_heard(s, np.zeros(16000, np.int16)))
    assert {"type": "alarm_stop", "quiet": True} in ws.sent
    import time
    assert any(abs(x.at - (time.time() + 300)) < 5 for x in hub.alarms.list())  # rings again in 5 minutes
    assert spoken[-1][0] == "Okay, snoozing for 5 minutes."


def test_other_words_while_ringing_are_ignored(settings):
    hub, s, ws, spoken, _ = setup(settings, "oh what a beautiful morning")
    ring(hub, s)
    asyncio.run(hub.ring_heard(s, np.zeros(16000, np.int16)))
    assert "alarm_stop" not in ws.types() and s.ringing


def test_alarm_mid_answer_pauses_and_then_carries_on(settings):
    hub, s, ws, spoken, _ = setup(settings)
    s.state, s.speaking_text = "speaking", "First bit. Second bit you haven't heard."
    s.speak_started, s.speak_total_s = 0.0, 1e9  # cut off right at the start of the second sentence
    import time
    s.speak_started = time.monotonic() - 0.3 - 1e9 * (len("First bit. ") + 1) / len(s.speaking_text)
    a = ring(hub, s)
    assert "stop_audio" in ws.types() and s.paused["what"] == "speaking"

    async def stopped():
        await hub.alarm_stopped(s, a.id, "voice")
        await asyncio.sleep(0.5)
    asyncio.run(stopped())
    text, _ = spoken[-1]
    assert text.endswith("Second bit you haven't heard.") and "orry" in text
    assert "First bit" not in text


def test_answer_ready_while_ringing_is_held_until_the_alarm_stops(settings, monkeypatch):
    hub, s, ws, spoken, _ = setup(settings)
    from buddy.brain import Reply

    async def handle(text, conv, ctx):
        await asyncio.sleep(0.2)
        return Reply("It's sunny tomorrow.", intent="chat")
    monkeypatch.setattr(hub.brain, "handle", handle)

    async def scene():
        turn = asyncio.create_task(hub.respond(s, "what's the weather tomorrow"))
        await asyncio.sleep(0.05)
        a = hub.alarms.add_alarm("07:00")
        await hub.alarm_fired(s, a.id, "alarm")
        await turn
        assert not spoken  # nothing said over the alarm
        await hub.alarm_stopped(s, a.id, "button")
        await asyncio.sleep(0.5)
    asyncio.run(scene())
    text, _ = spoken[-1]
    assert text.endswith("It's sunny tomorrow.") and text.startswith("Sorry")


def test_alarm_while_they_were_talking_asks_them_to_go_on(settings):
    hub, s, ws, spoken, _ = setup(settings)
    s.state = "listening"
    a = ring(hub, s)
    assert s.state == "idle"

    async def stopped():
        await hub.alarm_stopped(s, a.id, "voice")
        await asyncio.sleep(0.5)
    asyncio.run(stopped())
    text, then_listen = spoken[-1]
    assert "saying" in text and then_listen


def test_no_conversation_means_the_usual_alarm(settings):
    hub, s, ws, spoken, _ = setup(settings)
    a = ring(hub, s)
    assert s.paused is None
    asyncio.run(hub.alarm_stopped(s, a.id, "snooze"))
    assert not spoken
