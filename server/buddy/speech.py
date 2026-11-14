"""Speaking an answer while it's still being written.

Claude's words arrive a few at a time. They're gathered into sentences and sent on to the voice as
soon as each sentence is complete, and the voice's sound is played as it arrives, so Buddy starts
talking long before the whole answer exists.

ElevenLabs is reached over its WebSocket "stream-input" endpoint (text in as it comes, raw 16 kHz
PCM out, which the ESP32 plays with no decoding). Without ElevenLabs, or if that connection fails
before any sound, each sentence is spoken on its own through the usual voices (ElevenLabs over HTTP,
then the offline Piper voice).

To avoid a gap after the first sentence, playing starts once the first sentence's sound is in and
the second sentence has begun (or the answer has ended). Claude writes far faster than anyone
speaks, so after that the sound stays ahead of the playing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
import time
from typing import AsyncIterator

from . import timing
from .brain import speakable
from .tts import TTSUnavailable

log = logging.getLogger("buddy.speech")
ELEVEN_WS = "wss://api.elevenlabs.io/v1/text-to-speech/{voice}/stream-input"
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"')\]]*\s+")


class Sentences:
    """Turns a trickle of words into whole sentences."""

    def __init__(self):
        self.buf = ""

    def feed(self, text: str) -> list[str]:
        self.buf += text
        out = []
        while True:
            m = _SENTENCE_END.search(self.buf)
            if not m:
                break
            out.append(self.buf[:m.end()].strip())
            self.buf = self.buf[m.end():]
        return [s for s in out if s]

    def flush(self) -> list[str]:
        rest, self.buf = self.buf.strip(), ""
        return [rest] if rest else []


class LiveVoice:
    """One ElevenLabs WebSocket: open it early (warm), send sentences, read the sound back."""

    def __init__(self, settings):
        self.settings = settings
        self.ws = None
        self.opened_at = 0.0
        self.error = ""

    async def open(self) -> None:
        from websockets.asyncio.client import connect
        s = self.settings
        key = s.secret("ELEVENLABS_API_KEY")
        if not key:
            raise TTSUnavailable("no ElevenLabs key")
        url = (ELEVEN_WS.format(voice=s["elevenlabs_voice_id"])
               + f"?model_id={s['elevenlabs_model']}&output_format=pcm_16000&inactivity_timeout=60")
        self.ws = await asyncio.wait_for(connect(url, additional_headers={"xi-api-key": key},
                                                 max_size=2 ** 22, open_timeout=5), timeout=6)
        await self.ws.send(json.dumps({
            "text": " ",
            "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.0, "use_speaker_boost": True,
                               "speed": float(s["voice_speed"])},
            # Start making sound after 50 characters instead of the default 120: sooner first sound.
            "generation_config": {"chunk_length_schedule": [50, 90, 120, 160]},
        }))
        self.opened_at = time.monotonic()

    def fresh(self) -> bool:
        return self.ws is not None and time.monotonic() - self.opened_at < 50  # it closes after 60 s idle

    async def send(self, text: str, flush: bool = False) -> None:
        await self.ws.send(json.dumps({"text": text.strip() + " ", **({"flush": True} if flush else {})}))

    async def end(self) -> None:
        await self.ws.send(json.dumps({"text": ""}))  # no more text: finish and close

    async def audio(self) -> AsyncIterator[bytes]:
        carry = b""
        async for raw in self.ws:
            msg = json.loads(raw)
            if msg.get("audio"):
                data = carry + base64.b64decode(msg["audio"])
                cut = len(data) - len(data) % 2
                carry = data[cut:]
                if cut:
                    yield data[:cut]
            elif msg.get("error") or (msg.get("message") and not msg.get("isFinal")):
                self.error = str(msg.get("message") or msg.get("error"))[:200]
                raise TTSUnavailable(f"ElevenLabs live: {self.error}")
            if msg.get("isFinal"):
                break

    async def close(self) -> None:
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None


class SpeechStream:
    """Text goes in (bit by bit, or all at once); sound comes out, ready to play."""

    def __init__(self, hub, timer: "timing.TurnTimer | None" = None, live: LiveVoice | None = None):
        self.hub = hub
        self.timer = timer
        self.live = live  # a warm WebSocket, if one was opened when the wake word fired
        self.sentences = Sentences()
        self.queue: asyncio.Queue[str | None] = asyncio.Queue()
        self.text = ""  # everything said so far (for the chat and for "carry on")
        self.started = False  # the first words have arrived
        self.ended = False
        self.n_sentences = 0
        self.second = asyncio.Event()  # the second sentence has begun, or the answer is over
        self.then_listen = False
        self.engine = ""
        self.gaps = 0
        self.played = False  # some of it has gone to the speaker

    # ---------- text in ----------
    def feed(self, delta: str) -> None:
        """A few more words from Claude."""
        if not delta or self.ended:
            return
        if not self.started:
            self.started = True
            timing.mark("haiku_first_token")
        if self.n_sentences >= 1 and delta.strip():
            self.second.set()
        for s in self.sentences.feed(delta):
            self._sentence(s)

    def add_text(self, text: str) -> None:
        """A whole piece of text at once (a greeting, or an answer that wasn't streamed)."""
        self.feed(text if text.endswith(" ") else text + " ")

    def end(self) -> None:
        if self.ended:
            return
        for s in self.sentences.flush():
            self._sentence(s)
        self.ended = True
        self.second.set()
        self.queue.put_nowait(None)

    def _sentence(self, s: str) -> None:
        s = speakable(s)
        if not s:
            return
        self.n_sentences += 1
        if self.n_sentences == 1 and self.timer:
            self.timer.mark("first_sentence")
        self.text = f"{self.text} {s}".strip()
        self.queue.put_nowait(s)

    # ---------- sound out ----------
    async def pcm(self) -> AsyncIterator[bytes]:
        first = True
        async for chunk in self._sound():
            if first:
                first = False
                if self.timer:
                    self.timer.mark("tts_first_byte")
                # Don't start until the second sentence is on its way (or there isn't one), so Buddy
                # doesn't stop after the first sentence and wait. Never more than 1.5 s, though.
                try:
                    await asyncio.wait_for(self.second.wait(), timeout=1.5)
                except asyncio.TimeoutError:
                    pass
                self.played = True
            yield chunk

    async def _sound(self) -> AsyncIterator[bytes]:
        s = self.hub.settings
        eleven = getattr(self.hub.tts, "eleven", None)
        use_live = (s["tts_engine"] in ("auto", "elevenlabs") and eleven is not None and eleven.available)
        if use_live:
            got_any = False
            try:
                async for chunk in self._live_sound():
                    got_any = True
                    yield chunk
                self.engine = "elevenlabs live"
                return
            except Exception as e:  # noqa: BLE001 - fall back to one sentence at a time
                if got_any:
                    log.warning("ElevenLabs live stopped part way: %s", e)
                    return
                log.warning("ElevenLabs live unavailable (%s); speaking sentence by sentence", e)
        async for chunk in self._sentence_by_sentence():
            yield chunk

    async def _live_sound(self) -> AsyncIterator[bytes]:
        live, self.live = self.live, None
        if live is None or not live.fresh():
            if live is not None:
                await live.close()
            live = LiveVoice(self.hub.settings)
            await live.open()
        self.sent: list[str] = []
        timer = self.timer

        async def writer():
            first = True
            while True:
                s = await self.queue.get()
                if s is None:
                    await live.end()
                    return
                if first and timer:
                    timer.mark("tts_sent")
                self.sent.append(s)
                await live.send(s, flush=first)  # the first sentence goes straight away
                first = False

        w = asyncio.create_task(writer())
        try:
            async for chunk in live.audio():
                yield chunk
            await w
        finally:
            if not w.done():
                w.cancel()
            await live.close()

    async def _sentence_by_sentence(self) -> AsyncIterator[bytes]:
        """No live voice: each sentence on its own (the usual ElevenLabs request, or the offline voice).
        Sentences the live voice already took aren't lost: they're said again here."""
        todo = list(getattr(self, "sent", []))
        first = True
        while True:
            s = todo.pop(0) if todo else await self.queue.get()
            if s is None:
                break
            if first and self.timer:
                self.timer.mark("tts_sent")
            first = False
            async for chunk in self.hub.tts.stream(s):
                yield chunk
            self.engine = self.hub.tts.last_engine
