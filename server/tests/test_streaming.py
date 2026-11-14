"""Speaking Claude's answer while it's being written (speed change a)."""
import asyncio
import base64
import json
import time

import numpy as np
import pytest

from buddy import speech as speech_mod
from buddy.claude import ClaudeLLM
from buddy.hub import Hub, Session
from buddy.speech import Sentences, SpeechStream
from tests.fakes import FakeAPIs, FakeClaude, FakeSTT, claude_message, claude_text


def sse(parts: list[str], model="claude-haiku-5-5"):
    """A streamed Claude reply, the way the API sends it (server-sent events)."""
    def ev(kind, data):
        return f"event: {kind}\ndata: {json.dumps(data)}\n\n"
    body = ev("message_start", {"type": "message_start", "message": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model, "content": [],
        "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}})
    body += ev("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
    for p in parts:
        body += ev("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": p}})
    body += ev("content_block_stop", {"type": "content_block_stop", "index": 0})
    body += ev("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                                 "usage": {"output_tokens": 12}})
    body += ev("message_stop", {"type": "message_stop"})

    def step(request_body):
        assert request_body.get("stream") is True
        return body
    return step


class StreamingClaude(FakeClaude):
    def handler(self, request):
        import httpx2
        body = json.loads(request.content)
        self.requests.append(body)
        step = self.script.pop(0) if self.script else claude_message()
        if callable(step):
            out = step(body)
            if isinstance(out, str):
                return httpx2.Response(200, content=out.encode(), headers={"content-type": "text/event-stream"})
            status, payload = out
            return httpx2.Response(status, json=payload)
        return httpx2.Response(200, json=step)


def test_words_become_sentences():
    s = Sentences()
    assert s.feed("Paris is the cap") == []
    assert s.feed("ital of France. It's lov") == ["Paris is the capital of France."]
    assert s.feed("ely in spring! Want ") == ["It's lovely in spring!"]
    assert s.feed("more?") == []
    assert s.flush() == ["Want more?"]
    assert Sentences().feed("It's 7.30 now. ") == ["It's 7.30 now."]  # a number isn't a sentence end


def test_claude_streams_its_words(settings):
    claude = StreamingClaude()
    claude.script = [sse(["Paris is ", "the capital. ", "Want more?"])]
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    got = []
    res = asyncio.run(ClaudeLLM(settings, claude.client()).chat(
        [{"role": "user", "content": "capital of France?"}], on_text=got.append))
    assert got == ["Paris is ", "the capital. ", "Want more?"]
    assert res.content == "Paris is the capital. Want more?"


class FakeTTS:
    last_engine = "fake"
    eleven = None

    def __init__(self):
        self.said = []

    async def stream(self, text):
        self.said.append(text)
        await asyncio.sleep(0.01)
        yield b"\x01\x00" * (len(text) * 400)  # about 25 ms of sound a letter


class Board:
    def __init__(self):
        self.session, self.audio, self.msgs = None, 0, []

    async def send_text(self, text):
        m = json.loads(text)
        self.msgs.append(m)
        if m.get("type") == "audio_end":
            self.session.playback_done.set()

    async def send_bytes(self, data):
        self.audio += len(data) - 1


def streamed_hub(settings, claude):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-test")
    settings.update({"play_hold_ms": 100, "fact_check": True})
    apis = FakeAPIs()
    hub = Hub(settings, stt=FakeSTT(apis), claude_http=claude.client())
    hub.tts = FakeTTS()
    board = Board()
    s = Session(hub, board, "device", "esp32")
    board.session = s
    hub.sessions = {s.id: s}
    return hub, s, board, apis


def test_a_streamed_answer_is_spoken_sentence_by_sentence(settings):
    claude = StreamingClaude()
    claude.script = [sse(["The Eiffel ", "Tower is 330 metres tall. ", "It was built ", "in 1889."]),
                     claude_message(claude_text('{"verdict": "correct", "why": "right"}'))]
    hub, s, board, apis = streamed_hub(settings, claude)
    apis.transcripts = ["how tall is the eiffel tower"]
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.5))
    assert hub.tts.said == ["The Eiffel Tower is 330 metres tall.", "It was built in 1889."]
    assert board.audio > 0 and any(m.get("type") == "audio_end" for m in board.msgs)
    turn = next(e for e in hub.log if e["event"] == "turn")
    assert turn["streamed"] and turn["said"] == "The Eiffel Tower is 330 metres tall. It was built in 1889."
    assert not any(e["event"] == "corrected" for e in hub.log)  # the check after speaking passed
    assert len(claude.requests) == 2 and "stream" not in claude.requests[1]  # it did check


def test_a_wrong_streamed_answer_is_corrected_straight_after(settings):
    claude = StreamingClaude()
    claude.script = [sse(["The Eiffel Tower is 900 metres tall."]),
                     claude_message(claude_text('{"verdict": "wrong", "why": "it is 330 m"}')),
                     claude_message(claude_text("It's about 330 metres tall.")),  # second go (searched)
                     claude_message(claude_text('{"verdict": "correct", "why": "right"}'))]
    hub, s, board, apis = streamed_hub(settings, claude)
    apis.transcripts = ["how tall is the eiffel tower"]
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.5))
    fix = next(e for e in hub.log if e["event"] == "corrected")
    assert fix["said"].startswith("Actually, I've double-checked that.") and "330" in fix["said"]
    assert hub.tts.said[-1].startswith("Actually")


def test_quick_commands_still_work_when_streaming_is_on(settings):
    claude = StreamingClaude()
    hub, s, board, apis = streamed_hub(settings, claude)
    apis.transcripts = ["what time is it"]
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    assert claude.requests == [] and ":" in " ".join(hub.tts.said)


def test_switching_streaming_off_waits_for_the_whole_answer(settings):
    claude = StreamingClaude()
    claude.script = [claude_message(claude_text("Paris. Lovely city.")),
                     claude_message(claude_text('{"verdict": "correct", "why": "right"}'))]
    hub, s, board, apis = streamed_hub(settings, claude)
    settings.update({"stream_replies": False})
    apis.transcripts = ["what's the capital of France"]
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    assert "stream" not in claude.requests[0]
    assert hub.tts.said == ["Paris. Lovely city."]  # in one go


# ---------- the ElevenLabs WebSocket ----------
def test_live_voice_talks_to_elevenlabs_the_documented_way(settings, monkeypatch):
    from websockets.asyncio.server import serve
    got = []

    async def fake_eleven(ws):
        got.append(dict(ws.request.headers))
        async for raw in ws:
            msg = json.loads(raw)
            got.append(msg)
            if msg["text"] == "":
                await ws.send(json.dumps({"audio": None, "isFinal": True}))
                return
            if msg["text"].strip():
                await ws.send(json.dumps({"audio": base64.b64encode(b"\x02\x00" * 800).decode()}))

    async def run():
        async with serve(fake_eleven, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            monkeypatch.setattr(speech_mod, "ELEVEN_WS", f"ws://127.0.0.1:{port}/v1/text-to-speech/{{voice}}/stream-input")
            settings.set_secret("ELEVENLABS_API_KEY", "xi-test")
            hub = type("H", (), {})()
            hub.settings = settings
            hub.tts = type("T", (), {"eleven": type("E", (), {"available": True})(), "last_engine": ""})()
            sp = SpeechStream(hub)
            sp.add_text("Hello there. How are you?")
            sp.end()
            return [c async for c in sp.pcm()], sp

    chunks, sp = asyncio.run(run())
    assert sum(len(c) for c in chunks) == 2 * 1600 and sp.engine == "elevenlabs live"
    headers, init, first, second, close = got
    assert headers["xi-api-key"] == "xi-test"
    assert init["text"] == " " and init["generation_config"]["chunk_length_schedule"][0] == 50
    assert init["voice_settings"]["speed"] == 1.0
    assert first == {"text": "Hello there. ", "flush": True} and second == {"text": "How are you? "}
    assert close == {"text": ""}


def test_without_the_live_voice_it_falls_back_sentence_by_sentence(settings, monkeypatch):
    monkeypatch.setattr(speech_mod, "ELEVEN_WS", "ws://127.0.0.1:9/nothing/{voice}")  # nothing listening
    settings.set_secret("ELEVENLABS_API_KEY", "xi-test")
    tts = FakeTTS()
    tts.eleven = type("E", (), {"available": True})()
    hub = type("H", (), {"settings": settings, "tts": tts})()
    sp = SpeechStream(hub)
    sp.add_text("One. Two.")
    sp.end()

    async def run():
        return [c async for c in sp.pcm()]

    assert asyncio.run(run()) and tts.said == ["One.", "Two."]


# ---------- warm connections (speed change c) ----------
def test_hey_buddy_warms_up_claude_and_the_voice(settings, monkeypatch):
    opened, warmed = [], []

    class FakeLive:
        def __init__(self, settings):
            self.closed = False

        async def open(self):
            opened.append(self)

        def fresh(self):
            return True

        async def close(self):
            self.closed = True

    monkeypatch.setattr("buddy.hub.LiveVoice", FakeLive)
    claude = StreamingClaude()
    hub, s, board, apis = streamed_hub(settings, claude)
    hub.tts.eleven = type("E", (), {"available": True})()

    async def warm():
        warmed.append(1)
    hub.llm.warm = warm

    async def run():
        await s.start_listening(earcon=False)
        await asyncio.sleep(0.05)
        return await s.take_live()

    live = asyncio.run(run())
    assert warmed == [1] and opened == [live]  # both started while the question is still being asked


def test_warming_can_be_switched_off(settings):
    claude = StreamingClaude()
    hub, s, board, apis = streamed_hub(settings, claude)
    settings.update({"warm_connections": False})
    asyncio.run(s.start_listening(earcon=False))
    assert s.live_task is None
