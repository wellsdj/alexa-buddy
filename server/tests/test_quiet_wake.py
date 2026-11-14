"""A quiet "hey buddy": it must be checked and answered, and a quiet voice afterwards must still count."""
import asyncio

import numpy as np

from buddy.hub import Hub, Session
from buddy.vad import Endpointer
from tests.test_alarm_pause import FakeWS
from tests.fakes import FakeAPIs, FakeSTT


def setup(settings, heard):
    apis = FakeAPIs()
    apis.transcripts = [heard]
    settings.update({"stream_replies": False})  # these tests catch what's said through speak()
    hub = Hub(settings, stt=FakeSTT(apis))
    ws = FakeWS()
    s = Session(hub, ws, "device", "esp32")
    hub.sessions = {s.id: s}
    spoken = []

    async def speak(text, then_listen=False):
        spoken.append(text)

    s.speak = speak
    return hub, s, ws, spoken


def test_a_soft_hey_buddy_that_checks_out_gets_answered(settings):
    # This used to crash (a missing line in hub.py), so nothing happened at all.
    hub, s, ws, spoken = setup(settings, "Hey buddy, what time is it?")
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.5, silent="wake"))
    assert spoken and ":" in spoken[0]  # it said the time
    assert any(e["event"] == "wake_verified" for e in hub.log)


def test_a_soft_hey_buddy_on_its_own_waits_for_the_question(settings):
    hub, s, ws, spoken = setup(settings, "Hey buddy")
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0, silent="wake"))
    assert s.state == "listening" and not spoken


def test_a_quiet_voice_counts_after_the_wake_word():
    quiet = [0.38] * 20 + [0.05] * 30  # a soft voice, then a pause
    strict = Endpointer(silence_ms=800, no_speech_s=6.0, on=0.5, off=0.35)
    gentle = Endpointer(silence_ms=800, no_speech_s=6.0, on=0.3, off=0.25)
    assert strict.update(quiet) is None and not strict.started  # the old way: never noticed, then 6 s timeout
    assert gentle.update(quiet) == "end" and gentle.speech_seconds > 0.5
    assert strict.peak == 0.38  # the Activity log shows how close it came


def test_listening_uses_the_gentler_setting_but_silent_checks_dont(settings):
    hub, s, ws, spoken = setup(settings, "")
    asyncio.run(s.start_listening(earcon=False))
    assert s.endpointer.on == 0.3
    asyncio.run(s.start_listening(silent="room", earcon=False))
    assert s.endpointer.on == 0.5
