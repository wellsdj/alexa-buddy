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


def listen(hub, s, chunks, woken=False):
    """Feed the listening session one 32 ms chunk per speech score."""
    async def run():
        await s.start_listening(earcon=False)
        s.woken = woken
        for p in chunks:
            await s._on_audio(np.full(512, 300, np.int16), None, [p])
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.2)
    asyncio.run(run())


def test_words_are_written_down_at_the_first_pause(settings):
    hub, s, ws, spoken = setup(settings, "what time is it")
    listen(hub, s, [0.9] * 30 + [0.05] * 25)  # talk, then stop
    assert len(hub.stt.apis.stt_requests) == 1  # the early one was used: no second go
    assert spoken and ":" in spoken[0]


def test_carrying_on_after_a_pause_throws_the_early_words_away(settings):
    hub, s, ws, spoken = setup(settings, "what time")
    hub.stt.apis.transcripts.append("what time is it")
    listen(hub, s, [0.9] * 20 + [0.05] * 10 + [0.9] * 20 + [0.05] * 25)  # a pause mid-sentence
    assert spoken and ":" in spoken[0]  # the whole sentence was answered, not "what time"


def sounds(ws):
    """The sound effects sent to the board: the size of each one."""
    out, size = [], None
    for m in ws.sent:
        if m["type"] == "audio_start" and m.get("text") == "":
            size = 0
        elif m["type"] == "audio_end" and size is not None:
            out.append(m["bytes"])
            size = None
    return out


def test_a_clear_hey_buddy_plays_the_wake_sound_but_a_follow_up_doesnt(settings):
    from buddy.hub import WAKE_SOUND
    from types import SimpleNamespace
    hub, s, ws, spoken = setup(settings, "")

    async def run():
        s._quiet_before = lambda: True
        await s._on_audio(np.zeros(512, np.int16), SimpleNamespace(confident=True, score=0.9), [0.0])
        await asyncio.sleep(0.05)
        first = sounds(ws)
        s.state = "idle"
        await s.start_listening(earcon=True, no_speech_s=5.0)  # listening again after a question
        await asyncio.sleep(0.05)
        return first, sounds(ws)

    first, after = asyncio.run(run())
    assert first == [len(WAKE_SOUND)] and after == first


def test_saying_nothing_plays_the_nothing_heard_sound(settings):
    from buddy.hub import NOTHING_SOUND
    hub, s, ws, spoken = setup(settings, "")
    listen(hub, s, [0.0] * int(6.2 / 0.032), woken=True)  # "hey buddy", then 6 s of silence
    assert sounds(ws) == [len(NOTHING_SOUND)] and s.state == "idle"


def test_no_sound_when_a_follow_up_listen_hears_nothing(settings):
    hub, s, ws, spoken = setup(settings, "")
    listen(hub, s, [0.0] * int(6.2 / 0.032), woken=False)  # listening after Buddy asked something
    assert sounds(ws) == []


def test_no_wake_sound_when_cutting_in_while_buddy_talks(settings):
    from types import SimpleNamespace
    hub, s, ws, spoken = setup(settings, "")

    async def run():
        s.state = "speaking"
        await s._on_audio(np.zeros(512, np.int16), SimpleNamespace(confident=True, score=0.9), [0.0])
        await asyncio.sleep(0.05)
    asyncio.run(run())
    assert sounds(ws) == [] and s.state == "listening"


def test_sound_effects_can_be_switched_off(settings):
    hub, s, ws, spoken = setup(settings, "")
    settings.update({"sound_effects": False})
    listen(hub, s, [0.0] * int(6.2 / 0.032), woken=True)
    assert sounds(ws) == []
