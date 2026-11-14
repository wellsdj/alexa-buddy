"""Connections and the voice loop.

Every WebSocket client is a Session. The ESP32 is a session with role "device"; each
open web page is a session with role "ui" and can act as a virtual device (streaming its
microphone), so the web page exercises exactly the same code path as the hardware.

How Buddy decides you're talking to it:
- "Hey buddy" after a moment of quiet always wakes it (chime, light, listening).
- "Buddy" in the middle of someone's sentence doesn't wake it straight away: the sentence is
  transcribed silently and the context check (listening.py) decides.
- While Buddy is on, the desk unit's audio is transcribed sentence by sentence on this
  computer and the context check decides whether each one is for Buddy ("let me ask you a
  question" works without the wake word). Saying "off" stops that until "hey buddy" again.

Wire protocol (both directions over one WebSocket):
  client -> server  binary: 16 kHz mono int16 PCM from the microphone
                    text:   JSON control messages ({"type": ...})
  server -> client  binary: [1 byte utterance seq] + 16 kHz mono int16 PCM to play
                    text:   JSON control/events
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import random
import re
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import WebSocket

from . import intents, timing
from .alarms import Alarm, AlarmStore, DAY_NAMES, spoken_time
from .audio import Leveller, peak_db, rms_dbfs
from .brain import Brain, Conversation, Reply
from .claude import ClaudeLLM
from .config import Settings
from .listening import ContextJudge
from .llm import GroqLLM
from .memory import MemoryManager, MemoryStore
from .alarm_sounds import AlarmSounds
from .study import StudyStore, StudyWorker
from .music import MusicPlayer
from .spotify import Spotify
from .speech import LiveVoice, SpeechStream
from .stt import LocalSTT, STTError
from .tts import ElevenLabsTTS, TTSRouter, TTSUnavailable
from .vad import CHUNK as VAD_CHUNK
from .vad import Endpointer, StreamingVad
from .voiceid import VoiceID, VoiceIDError
from .wakeword import WakeDetector
from .weather import Weather

log = logging.getLogger("buddy.hub")
SR = 16000
BYTES_PER_S = SR * 2
SORRY = ["Sorry about that!", "Sorry, that was my alarm.", "Sorry about that."]
AS_I_WAS_SAYING = ["Sorry about that! As I was saying,", "Oh sorry, where was I? Right,",
                   "Sorry about that. Anyway, as I was saying,"]
YOU_WERE_SAYING = ["Oh sorry, you were saying?", "Sorry about that! What were you saying?", "Sorry! Go on, you were saying?"]
QUIET_INTENTS = ("off", "stop", "empty", "snooze", "error", "llm_error", "repeat", "carry_on", "more_memory")
STILL_WORKING = ["Hmm, one sec.", "Bear with me.", "Just a sec.", "Hang on a moment."]
# Played once when the desk unit is switched on: 16 kHz mono 16-bit.
# Sound effects, 16 kHz mono 16-bit (made from your mp3s: silence trimmed, peaks at -3 dB like the voice).
_SOUNDS = Path(__file__).resolve().parent.parent / "sounds"


def _sound(name: str) -> bytes:
    f = _SOUNDS / f"{name}.pcm"
    return f.read_bytes() if f.exists() else b""


STARTUP_SOUND = _sound("startup")  # the desk unit has just been switched on
WAKE_SOUND = _sound("wake")  # it's sure it heard "hey buddy" (not when it listens again for an answer)
NOTHING_SOUND = _sound("nothing_heard")  # it was listening, but you didn't say anything


class Session:
    def __init__(self, hub: "Hub", ws: WebSocket, role: str, name: str):
        self.hub = hub
        self.ws = ws
        self.role = role
        self.name = name
        self.id = f"{role}-{name}-{id(self) % 10000}"
        self.connected_at = time.time()
        self.send_lock = asyncio.Lock()
        self.state = "idle"
        self.hands_free = role == "device"
        self.detector: WakeDetector | None = None
        self.vad: StreamingVad | None = None
        self.preroll: collections.deque = collections.deque()
        self.preroll_samples = 0
        self.vad_history: collections.deque = collections.deque(maxlen=int(3.0 * SR / VAD_CHUNK))
        self.capture: list[np.ndarray] = []
        self.endpointer: Endpointer | None = None
        self.silent: str | None = None  # listening without telling anyone: "wake" | "context" | "room"
        self.mentioned = False  # the wake word fired somewhere inside the sentence being captured
        self.captured = 0  # samples heard since this capture started (not counting the pre-roll)
        self.quiet_until = 0.0  # don't take Buddy's own voice for someone talking
        self.ptt = False
        self.follow = False
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=400)
        self.worker: asyncio.Task | None = None
        self.speak_task: asyncio.Task | None = None
        self.mic_test = False  # being tested: transcribe everything live for the Mic test page
        self.mic_test_viewer: "Session | None" = None
        self.mic_test_until = 0.0
        self.enroll_name = ""  # Your voice tab: add what's said in the mic test to this person's voice
        self.speaker: dict = {}  # who said the sentence being answered
        self.early: dict | None = None  # words being written down at the first pause (before the end)
        self.reply_task: asyncio.Task | None = None  # the answer being worked out (cancelled if cut off)
        self.sound_until = 0.0  # a sound effect is playing until then
        self.barge_buf: list[np.ndarray] = []  # the last 2 s heard while Buddy talks
        self.barge_voiced = 0.0
        self.barge_checking = False
        self.live_task: asyncio.Task | None = None  # an ElevenLabs connection opened early (warm)
        self.turn_audio: np.ndarray | None = None  # what they said, to learn their voice from
        self.mt_buf: list[np.ndarray] = []
        self.mt_silence = 0
        self.mt_busy = False
        self.mt_last_partial = 0.0
        self.mt_peak = 0.0
        self.mt_clipped = 0
        self.speaking_text = ""  # the answer being spoken now
        self.speak_started: float | None = None
        self.speak_total_s = 0.0
        self.interrupted_text = ""  # the answer they cut into (its echo may open their next words)
        self.interrupted_at = 0.0
        self.relistened = False  # already gave them a second go after not making out their words
        self.interim_task: asyncio.Task | None = None  # a "one sec, I'll check" line still playing
        self.seq = 0
        self.playback_done = asyncio.Event()
        self.ringing: str | None = None
        # An alarm that breaks into a conversation pauses it, and Buddy picks it up afterwards.
        self.paused: dict | None = None
        self.turns = 0  # answers given, to tell whether they moved on while the alarm rang
        self.last_intent = ""
        self.turn_running = 0
        # While an alarm rings, listen for "stop" or "snooze" without "hey buddy".
        self.ring_buf: list[np.ndarray] = []
        self.ring_speech = 0.0
        self.ring_quiet = 0.0
        self.ring_busy = False
        self.level_db = -120.0
        self.last_level_push = 0.0
        self.info: dict = {}
        self.verify_times: collections.deque = collections.deque(maxlen=200)

    # ---------- sending ----------
    async def send_json(self, msg: dict) -> None:
        try:
            async with self.send_lock:
                await self.ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001 - a dead socket is handled by the receive loop
            pass

    async def send_audio(self, seq: int, pcm: bytes) -> None:
        async with self.send_lock:
            await self.ws.send_bytes(bytes([seq]) + pcm)

    async def set_state(self, state: str, force: bool = False) -> None:
        if state == self.state and not force:
            return
        self.state = state
        if self.role == "device":  # music down while Buddy listens and talks, back up after
            if state in ("listening", "thinking", "speaking"):
                self.hub._soon(self.hub.spotify.duck())
            elif state == "idle":
                self.hub._soon(self.hub.spotify.unduck())
        await self.send_json({"type": "state", "state": state})
        await self.hub.broadcast_ui({"type": "event", "kind": "state", "session": self.id,
                                     "role": self.role, "state": state}, exclude=self)

    async def earcon(self, name: str) -> None:
        if name == "nospeech":
            await self.play_sound(NOTHING_SOUND)  # listened, but you said nothing
            return
        # No beeps when Buddy listens, finishes hearing you or can't hear you: the light shows it.
        # Only alarm and reminder chimes still sound.
        if name in ("chime",):
            await self.send_json({"type": "earcon", "name": name})

    async def play_startup_sound(self) -> None:
        """The "I'm on" sound, streamed like speech so it follows Buddy's volume."""
        await self.play_sound(STARTUP_SOUND)

    async def wake_sound(self) -> None:
        """Buddy is sure it heard "hey buddy"."""
        await self.play_sound(WAKE_SOUND)

    async def play_sound(self, pcm: bytes) -> None:
        """A short sound effect through Buddy's speaker (follows the volume, doesn't change the light)."""
        if not pcm or not self.hub.settings["sound_effects"]:
            return
        self.seq = (self.seq + 1) % 256
        seq = self.seq
        await self.send_json({"type": "audio_start", "seq": seq, "rate": SR, "text": ""})
        for i in range(0, len(pcm), 3200):
            await self.send_audio(seq, pcm[i:i + 3200])
        await self.send_json({"type": "audio_end", "seq": seq, "bytes": len(pcm)})
        self.sound_until = time.monotonic() + len(pcm) / BYTES_PER_S

    # ---------- mic test ----------
    async def _mic_test_audio(self, pcm: np.ndarray, probs: list[float]) -> None:
        """Live transcription for the Mic test page: partial text while you talk, final text after."""
        if time.time() > self.mic_test_until or not self.mic_test_viewer:
            self.mic_test = False
            return
        peak = float(np.abs(pcm.astype(np.int32)).max()) if len(pcm) else 0.0
        self.mt_peak = max(self.mt_peak * 0.9, peak)
        self.mt_clipped += int(np.sum(np.abs(pcm.astype(np.int32)) >= 32000))
        voiced = any(p >= 0.5 for p in probs)
        if voiced or self.mt_buf:
            self.mt_buf.append(pcm)
        self.mt_silence = 0 if voiced else self.mt_silence + len(pcm)
        stats = {"db": round(self.level_db, 1),
                 "peak_db": round(20 * np.log10(max(self.mt_peak, 1.0) / 32768), 1), "clipped": self.mt_clipped}
        audio_s = sum(len(b) for b in self.mt_buf) / SR
        if self.mt_buf and (self.mt_silence > SR * 0.7 or audio_s > 12):
            audio = np.concatenate(self.mt_buf)
            self.mt_buf, self.mt_silence = [], 0
            asyncio.create_task(self._mic_test_send(audio, final=True, stats=stats))
        elif self.mt_buf and not self.mt_busy and time.time() - self.mt_last_partial > 0.8 and audio_s > 0.5:
            self.mt_last_partial = time.time()
            asyncio.create_task(self._mic_test_send(np.concatenate(self.mt_buf), final=False, stats=stats))
        elif time.time() - self.mt_last_partial > 0.5 and not self.mt_buf:
            self.mt_last_partial = time.time()
            await self.mic_test_viewer.send_json({"type": "event", "kind": "mic_test", **stats})

    async def _mic_test_send(self, audio: np.ndarray, final: bool, stats: dict) -> None:
        if not final:
            self.mt_busy = True
        t0 = time.monotonic()
        try:
            text = await self.hub.stt.transcribe(audio, "", max(0.5, len(audio) / SR))
        except STTError as e:
            text = f"[ears error: {e}]"
        finally:
            if not final:
                self.mt_busy = False
        ms = int((time.monotonic() - t0) * 1000)
        who: dict = {}
        if final and self.enroll_name and self.mic_test_viewer:
            source = "desk unit mic" if self.role == "device" else "browser mic"
            try:
                res = await asyncio.to_thread(self.hub.voiceid.add_audio, self.enroll_name, audio, source)
                await self.mic_test_viewer.send_json({"type": "event", "kind": "voice_enroll", **res})
            except VoiceIDError as e:
                await self.mic_test_viewer.send_json({"type": "event", "kind": "voice_enroll", "error": str(e)})
        elif final:
            who = await self.hub.who_spoke(audio)
        if self.mic_test_viewer:
            await self.mic_test_viewer.send_json({
                "type": "event", "kind": "mic_test", "text": text, "final": final, "ms": ms,
                "engine": self.hub.stt.status().get("engine", ""), "seconds": round(len(audio) / SR, 1),
                **({"speaker": who.get("name", ""), "learning": who.get("learning", ""),
                    "voice_score": who.get("score", 0.0)} if who else {}), **stats})

    # ---------- audio in ----------
    def feed(self, pcm_bytes: bytes) -> None:
        if not self.hands_free and not self.ptt and self.state != "listening" and not self.mic_test:
            return
        pcm = np.frombuffer(pcm_bytes[: len(pcm_bytes) - len(pcm_bytes) % 2], dtype=np.int16)
        try:
            self.inbox.put_nowait(pcm)
        except asyncio.QueueFull:
            pass  # we're behind; dropping audio beats growing latency forever
        if self.worker is None or self.worker.done():
            self.worker = asyncio.create_task(self._audio_worker())

    async def _audio_worker(self) -> None:
        if self.detector is None:
            self.detector = WakeDetector()
            self.vad = StreamingVad()
        while True:
            pcm = await self.inbox.get()
            parts = [pcm]
            while not self.inbox.empty():
                parts.append(self.inbox.get_nowait())
            pcm = np.concatenate(parts)
            s = self.hub.settings
            try:
                # Work in 120 ms slices so that, even when we fall behind and a big batch
                # arrives at once, the pre-roll is cut relative to the moment of the wake word.
                for start in range(0, len(pcm), 1920):
                    piece = pcm[start:start + 1920]
                    run_wake = self.hands_free and not self.mic_test and (self.state != "listening" or bool(self.silent))
                    wake, probs = await asyncio.to_thread(
                        self._analyze, piece, run_wake, float(s["wake_threshold"]), float(s["wake_verify_threshold"]))
                    await self._on_audio(piece, wake, probs)
            except Exception:  # noqa: BLE001
                log.exception("audio processing failed")

    def _analyze(self, pcm, run_wake: bool, thr: float, verify_thr: float):
        wake = self.detector.feed(pcm, thr, verify_thr) if run_wake else None
        probs = self.vad.feed(pcm)
        return wake, probs

    def _push_preroll(self, pcm: np.ndarray) -> None:
        self.preroll.append(pcm)
        self.preroll_samples += len(pcm)
        while self.preroll_samples - len(self.preroll[0]) > 3 * SR:
            self.preroll_samples -= len(self.preroll.popleft())

    def _take_preroll(self, seconds: float) -> np.ndarray:
        if not self.preroll:
            return np.zeros(0, dtype=np.int16)
        audio = np.concatenate(list(self.preroll))
        return audio[-int(seconds * SR):]

    async def _on_audio(self, pcm: np.ndarray, wake, probs: list[float]) -> None:
        self._push_preroll(pcm)
        self.vad_history.extend(probs)
        now = time.time()
        self.level_db = max(self.level_db - 3, rms_dbfs(pcm))
        if now - self.last_level_push > 0.25:
            self.last_level_push = now
            score = self.detector.take_peak() if self.detector else 0.0
            await self.hub.broadcast_ui({"type": "event", "kind": "level", "session": self.id,
                                         "db": round(self.level_db, 1), "wake": round(score, 3)})

        if self.mic_test:
            await self._mic_test_audio(pcm, probs)
            return  # Buddy doesn't react while its mic is being tested

        if self.state == "listening":
            self.capture.append(pcm)
            self.captured += len(pcm)
            if self.silent and wake and wake.confident:
                # Measured in audio, not clock time: audio can arrive in bursts over Wi-Fi.
                if self.silent == "room" and self.captured / SR > 1.8:
                    self.mentioned = True  # "buddy" mid-sentence: let the context check decide
                else:
                    await self._promote(wake)  # "hey buddy" started the sentence: wake for real
            if self.ptt:
                return
            result = self.endpointer.update(probs)
            if result:
                await self._finish_capture(result)
            else:
                self._maybe_transcribe_early()
            return

        if not self.hands_free:
            return
        if self.ringing and self.role == "device":
            self._ring_listen(pcm, probs)
        if self.state == "speaking":
            self._barge_listen(pcm, probs)
        if wake is not None:
            quiet = self._quiet_before()
            # While Buddy talks its own voice fills the room, so a clear "hey buddy" interrupts anyway.
            if wake.confident and (quiet or self.state == "speaking") and (
                    self.state in ("idle", "speaking") or self.ringing):
                await self.hub.broadcast_ui({"type": "event", "kind": "wake", "session": self.id,
                                             "score": round(wake.score, 3), "confident": True})
                if self.state == "speaking":
                    await self.stop_speaking()
                if self.ringing:
                    await self.send_json({"type": "alarm_stop"})
                self.hub.set_awake(True)
                self.hub._soon(self.wake_sound())
                await self.start_listening(preroll_s=0.8, earcon=True)
                return
            if self.state == "idle" and self._verify_allowed(now):
                self.verify_times.append(now)
                if quiet:  # a soft or unclear "hey buddy" out of nowhere: check what was said
                    await self.start_listening(preroll_s=2.2, silent="wake", earcon=False)
                else:  # said mid-sentence: hear the whole sentence, then judge it in context
                    await self.start_listening(preroll_s=3.0, silent="context", earcon=False)
                    self.mentioned = True
                return
        if (self.role == "device" and self.state == "idle" and not self.ringing and now >= self.quiet_until
                and self.hub.room_listening_active() and probs and max(probs) > 0.6):
            await self.start_listening(preroll_s=0.6, silent="room", earcon=False, no_speech_s=3.0)

    def _maybe_transcribe_early(self) -> None:
        """At the first short pause, start writing down what's been said so far. If they don't carry on,
        the words are ready the moment the end-of-speech wait is over; if they do, it's thrown away."""
        ep, s = self.endpointer, self.hub.settings
        if not (ep and ep.started and s["early_transcribe"]) or not self.capture:
            return
        if ep.silent_run * ep.chunk_s * 1000 < 250:
            return
        if self.early and self.early["speech_chunks"] == ep.speech_chunks:
            return  # already started on exactly this
        if self.early:
            self.early["task"].cancel()  # an earlier pause: they carried on talking after it
        audio = np.concatenate(self.capture)
        self.early = {"speech_chunks": ep.speech_chunks, "samples": len(audio),
                      "task": asyncio.create_task(self.hub.stt.transcribe(audio, "", ep.speech_seconds))}

    def _take_early(self) -> "asyncio.Task | None":
        """The early transcription, if nothing new was said after it started."""
        early, self.early = self.early, None
        if not early:
            return None
        if self.endpointer and early["speech_chunks"] == self.endpointer.speech_chunks:
            return early["task"]
        early["task"].cancel()  # they carried on talking: it's missing words
        return None

    def _ring_listen(self, pcm: np.ndarray, probs: list[float]) -> None:
        """While an alarm rings: when someone says something short, check whether it was "stop" or
        "snooze". Only those words count, so the alarm's own sound can't set anything off."""
        self.ring_buf.append(pcm)
        while sum(len(x) for x in self.ring_buf) > 3 * SR:
            self.ring_buf.pop(0)
        if probs and max(probs) > 0.5:
            self.ring_speech += len(pcm) / SR
            self.ring_quiet = 0.0
        else:
            self.ring_quiet += len(pcm) / SR
        # A short burst of speech that has just ended, or 2 s of sound (talking over the alarm).
        if (self.ring_speech >= 0.25 and self.ring_quiet >= 0.35) or self.ring_speech >= 2.0:
            audio = np.concatenate(self.ring_buf)
            self.reset_ring_ears()
            if not self.ring_busy:
                self.ring_busy = True
                asyncio.create_task(self.hub.ring_heard(self, audio))
        elif self.ring_quiet > 1.5:
            self.ring_speech = 0.0

    def _barge_listen(self, pcm: np.ndarray, probs: list[float]) -> None:
        """While Buddy talks: someone talking over it for over half a second, whose voice Buddy knows,
        cuts it off (no "hey buddy" needed). Its own voice comes back through the mic too, which is why
        the voice has to be recognised: only people saved on the Your voice tab can do this."""
        s = self.hub.settings
        if not s["barge_in"] or not self.hub.voiceid.people:
            return
        self.barge_buf.append(pcm)
        while sum(len(x) for x in self.barge_buf) > 2 * SR:
            self.barge_buf.pop(0)
        talking = bool(probs) and max(probs) > 0.6
        step = len(pcm) / SR
        self.barge_voiced = self.barge_voiced + step if talking else max(0.0, self.barge_voiced - step / 2)
        if self.barge_voiced >= 0.6 and not self.barge_checking:
            self.barge_checking = True
            asyncio.create_task(self._barge_check(np.concatenate(self.barge_buf)))

    async def _barge_check(self, audio: np.ndarray) -> None:
        try:
            who = await self.hub.who_spoke(audio)
        finally:
            self.barge_checking = False
        if who.get("name") and self.state == "speaking":
            self.hub.record("interrupted", session=self.id, by=who["name"], score=who.get("score"))
            await self.stop_speaking()
            # Listen from a moment ago, so the words they said over Buddy are kept.
            await self.start_listening(preroll_s=1.6, earcon=False)
        else:
            self.barge_voiced = 0.0  # Buddy's own voice, or someone it doesn't know: carry on

    def reset_ring_ears(self) -> None:
        self.ring_buf, self.ring_speech, self.ring_quiet = [], 0.0, 0.0

    async def _promote(self, wake) -> None:
        """A silent capture turned out to start with "hey buddy": chime and light up as usual."""
        self.silent = None
        self.hub.set_awake(True)
        self.warm_up()
        self.hub._soon(self.wake_sound())
        await self.earcon("wake")
        await self.set_state("listening", force=True)
        await self.hub.broadcast_ui({"type": "event", "kind": "wake", "session": self.id,
                                     "score": round(wake.score, 3), "confident": True})

    def _quiet_before(self) -> bool:
        """Was it quiet just before the wake word? It fires ~0.9 s into "hey buddy", so look at the
        1.3 s before that. Mostly silence there means "hey buddy" came out of nowhere."""
        probs = list(self.vad_history)
        phrase, before = int(0.9 * SR / VAD_CHUNK), int(1.3 * SR / VAD_CHUNK)
        window = probs[-(phrase + before):-phrase] if len(probs) > phrase else []
        if not window:
            return True
        return sum(p > 0.5 for p in window) / len(window) < 0.3

    def _verify_allowed(self, now: float) -> bool:
        recent = [t for t in self.verify_times if now - t < 3600]
        if recent and now - recent[-1] < 4:
            return False
        return len(recent) < 300

    # ---------- listening ----------
    async def start_listening(self, preroll_s: float = 0.0, silent: str | None = None, earcon: bool = True,
                              ptt: bool = False, no_speech_s: float = 6.0) -> None:
        s = self.hub.settings
        self.silent = silent
        self.mentioned = False
        self.captured = 0
        if self.early:
            self.early["task"].cancel()
        self.early = None
        self.ptt = ptt
        pre = self._take_preroll(preroll_s) if preroll_s else np.zeros(0, dtype=np.int16)
        self.capture = [pre] if len(pre) else []
        # Listening for real (the light is on): a quieter voice still counts. Silent captures (checking
        # room talk) keep the stricter default so the TV or the radio doesn't keep them going.
        on = 0.5 if silent else min(0.5, max(0.1, float(s["listen_sensitivity"])))
        self.endpointer = Endpointer(silence_ms=int(s["end_of_speech_ms"]), no_speech_s=no_speech_s,
                                     max_s=float(s["max_listen_s"]) + preroll_s, on=on, off=min(0.35, on - 0.05))
        if preroll_s:
            n = int(preroll_s * SR / VAD_CHUNK)
            self.endpointer.update(list(self.vad_history)[-n:])
        if self.detector and not silent:
            self.detector.reset()
        if earcon:
            await self.earcon("wake")
        if not silent:
            self.warm_up()
            await self.set_state("listening")
        else:
            self.state = "listening"  # silent: no LED, no chime unless it turns out to be for Buddy

    async def end_ptt(self) -> None:
        if self.state == "listening" and self.ptt:
            await self._finish_capture("end")

    async def _finish_capture(self, reason: str) -> None:
        audio = np.concatenate(self.capture) if self.capture else np.zeros(0, dtype=np.int16)
        silent, self.silent, self.ptt = self.silent, None, False
        mentioned, self.mentioned = self.mentioned, False
        speech_s = self.endpointer.speech_seconds if self.endpointer else 0.0
        # When you actually stopped talking: the end-of-speech wait is the trailing quiet it just waited for.
        now = time.monotonic()
        quiet_s = self.endpointer.silent_run * self.endpointer.chunk_s if (self.endpointer and reason == "end") else 0.0
        timer = timing.TurnTimer(speech_end=now - quiet_s, endpoint=now) if self.hub.settings["timing_log"] else None
        early = self._take_early()
        self.capture = []
        if reason == "timeout" or len(audio) < SR * 0.3:
            if self.early:
                self.early["task"].cancel()
                self.early = None
            if not silent and self.endpointer:
                self.hub.record("no_speech", session=self.id, waited_s=round(len(audio) / SR, 1),
                                loudest_speech=round(self.endpointer.peak, 2),
                                needed=self.endpointer.on)
            self.state = "idle"
            await self.send_json({"type": "state", "state": "idle"})
            if not silent:
                await self.earcon("nospeech")
            return
        if silent:
            self.state = "thinking"  # still silent until the transcript shows it's for Buddy
        else:
            await self.set_state("thinking")
            await self.earcon("end")
        asyncio.create_task(self.hub.process_audio(self, audio, speech_s, silent, mentioned, timer=timer, early=early))

    # ---------- warm connections ----------
    def warm_up(self) -> None:
        """Someone's about to ask something: connect to Claude and the voice now, while they talk,
        so neither connection has to be made after the question is ready."""
        s = self.hub.settings
        if not s["warm_connections"]:
            return
        self.hub._soon(self.hub.llm.warm())
        eleven = getattr(self.hub.tts, "eleven", None)
        if not (s["stream_replies"] and s["tts_engine"] in ("auto", "elevenlabs") and eleven is not None
                and eleven.available):
            return
        if self.live_task is not None and not self.live_task.done():
            return  # already connecting
        if self.live_task is not None and self.live_task.done() and not self.live_task.cancelled() \
                and self.live_task.exception() is None and self.live_task.result().fresh():
            return  # still open from a moment ago
        self._drop_live()

        async def connect() -> LiveVoice:
            live = LiveVoice(s)
            await live.open()
            return live
        self.live_task = asyncio.create_task(connect())

    async def take_live(self) -> LiveVoice | None:
        """The warm voice connection, if there is a good one (waiting a moment if it's still opening)."""
        task, self.live_task = self.live_task, None
        if task is None:
            return None
        try:
            live = await asyncio.wait_for(asyncio.shield(task), timeout=1.0)
        except Exception:  # noqa: BLE001 - not ready or failed: the stream opens its own
            task.add_done_callback(lambda t: t.cancelled() or t.exception() or self.hub._soon(t.result().close()))
            return None
        return live if live.fresh() else None

    def _drop_live(self) -> None:
        task, self.live_task = self.live_task, None
        if task is None:
            return
        if task.done():
            if not task.cancelled() and task.exception() is None:
                self.hub._soon(task.result().close())
        else:
            task.cancel()

    # ---------- speaking ----------
    async def interim(self, text: str) -> None:
        """Say a short "one sec" line while the real answer is still being worked on. Doesn't wait."""
        if self.interim_task and not self.interim_task.done():
            return
        await self.hub.broadcast_ui({"type": "event", "kind": "transcript", "session": self.id,
                                     "role": "assistant", "text": text, "intent": "one_sec"})
        self.interim_task = asyncio.create_task(self._speak(text, False, after="thinking"))

    async def speak(self, text: str, then_listen: bool = False) -> None:
        if self.interim_task and not self.interim_task.done():
            try:  # let the "one sec" line finish rather than cutting it off
                await asyncio.wait_for(asyncio.shield(self.interim_task), timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        self.speak_task = asyncio.create_task(self._speak(text, then_listen))
        try:
            await self.speak_task
        except asyncio.CancelledError:
            pass

    async def _speak(self, text: str, then_listen: bool, after: str = "idle") -> None:
        if not text:
            await self.set_state("idle")
            return
        await self._play(self.hub.tts.stream(text), lambda: text, lambda: then_listen, after, hold_s=0.5,
                         engine=lambda: self.hub.tts.last_engine)

    async def speak_stream(self, speech) -> None:
        """Speak an answer while it's still being written (speech.SpeechStream)."""
        if self.interim_task and not self.interim_task.done():
            try:  # let the "one sec" line finish rather than cutting it off
                await asyncio.wait_for(asyncio.shield(self.interim_task), timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        hold = max(0.05, int(self.hub.settings["play_hold_ms"]) / 1000)
        self.speak_task = asyncio.create_task(self._play(
            speech.pcm(), lambda: speech.text, lambda: speech.then_listen, "idle", hold_s=hold,
            engine=lambda: speech.engine, gaps=speech))
        try:
            await self.speak_task
        except asyncio.CancelledError:
            pass

    async def _play(self, pcm_iter, text_of, then_listen_of, after: str, hold_s: float, engine, gaps=None) -> None:
        """Send sound to the speaker as it arrives. Nothing happens (no light, no "speaking") until the
        first sound is ready. text_of / then_listen_of are read as they change (streamed answers grow)."""
        seq = None
        answer = after == "idle"  # a real answer (not a "one sec"): remember it, in case they cut in
        sent = 0
        t0 = time.monotonic()
        # The board holds 1.5 s of sound. Keep it about 0.8 s ahead: enough to ride out Wi-Fi
        # hiccups, never so much that it overflows and drops pieces mid-word.
        lead = 0.8
        leveller = Leveller()  # same loudness whichever voice spoke
        held = bytearray()  # the first bit of sound, collected before playing starts
        started_at = None  # the clock starts when the first sound is sent, not before the voice arrives
        timer = timing.current.get() if answer else None

        async def begin() -> int:
            nonlocal seq
            wait = self.sound_until - time.monotonic()
            if wait > 0:  # let a sound effect finish rather than cut it off
                await asyncio.sleep(min(wait, 2.0))
            self.seq = (self.seq + 1) % 256
            seq = self.seq
            self.playback_done.clear()
            self.barge_buf, self.barge_voiced = [], 0.0
            await self.set_state("speaking")
            await self.send_json({"type": "audio_start", "seq": seq, "rate": SR, "text": text_of()})
            if answer:
                self.speaking_text, self.speak_started, self.speak_total_s = text_of(), None, 0.0
            return seq

        async def send(pcm: bytes, final: bool = False) -> None:
            nonlocal sent, started_at
            if started_at is None:
                held.extend(pcm)
                if len(held) < BYTES_PER_S * hold_s and not final:
                    return  # collect a little first, so a slow start doesn't stutter
                if not held:
                    return
                pcm, started_at = bytes(held), time.monotonic()
                await begin()
                if answer:
                    self.speak_started = started_at
                if timer:
                    # The board plays as soon as this arrives (a few tens of ms over Wi-Fi).
                    timer.mark("first_audio", started_at)
                    self.hub.finish_timing(timer)
            for i in range(0, len(pcm), 3200):  # 100 ms frames
                part = pcm[i:i + 3200]
                ahead = sent / BYTES_PER_S - (time.monotonic() - started_at)
                if ahead > lead:
                    await asyncio.sleep(ahead - lead)
                elif ahead < -0.05 and gaps is not None and sent:
                    gaps.gaps += 1  # the sound ran out mid-answer: a pause the listener can hear
                await self.send_audio(seq, part)
                sent += len(part)
                if answer:
                    self.speaking_text = text_of()
                    self.speak_total_s = sent / BYTES_PER_S

        try:
            if timer and gaps is None:
                timer.mark("tts_sent")
            try:
                async for chunk in pcm_iter:
                    if timer and gaps is None:
                        timer.mark("tts_first_byte")
                    await send(leveller.process(chunk))
            finally:
                if hasattr(pcm_iter, "aclose"):
                    await pcm_iter.aclose()  # cut off: close the voice connection now, not later
            await send(leveller.flush(), final=True)
            t0 = started_at or t0
            if started_at is not None:
                self.hub.record("voice", engine=engine(), to=self.role,
                                volume=self.hub.brain.volume, peak_in_db=peak_db(leveller.peak_in),
                                peak_out_db=peak_db(leveller.peak_out),
                                dropped_on_board=self.info.get("dropped_spk"),
                                **({"gaps": gaps.gaps} if gaps is not None else {}))
                if timer and gaps is not None:
                    timing.note("gaps", gaps.gaps)
        except TTSUnavailable as e:
            log.warning("no TTS: %s", e)
            if seq is None:
                await begin()
            text = text_of()
            await self.send_json({"type": "say", "seq": seq, "text": text})
            if self.role == "device":
                await self.earcon("error")
            sent = int(len(text) / 14 * BYTES_PER_S)  # rough speaking time for the browser voice
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("speaking failed")
        if seq is None:  # nothing to say after all
            await self.set_state(after if after != "idle" else "idle")
            return
        await self.send_json({"type": "audio_end", "seq": seq, "bytes": sent})
        remaining = max(0.0, sent / BYTES_PER_S - (time.monotonic() - t0))
        try:
            await asyncio.wait_for(self.playback_done.wait(), timeout=remaining + 4.0)
        except asyncio.TimeoutError:
            pass
        self.quiet_until = time.time() + 1.0  # the last of Buddy's voice is still in the room
        self.hub.last_spoke_at = time.time()
        if then_listen_of() and self.hands_free:
            await self.start_listening(earcon=True, no_speech_s=5.0)
        else:
            await self.set_state(after)  # "thinking" after a "one sec" line: the answer is on its way

    def _where_we_got_to(self) -> None:
        """Cut off mid-answer: remember what was being said (to tell its echo from their words) and
        what was still to come (for "carry on")."""
        text, started = self.speaking_text, self.speak_started
        if not text or self.state != "speaking":
            return
        self.interrupted_text, self.interrupted_at = text, time.time()
        if started is None:
            self.hub.conv.interrupted = text  # cut off before it began: all of it is still to come
            return
        total = max(self.speak_total_s, len(text) / 14.0)  # ~14 characters a second, if still arriving
        played = max(0.0, time.monotonic() - started - 0.3)
        upto = int(len(text) * min(1.0, played / total))
        # Back up to the start of the sentence being said, so "carry on" picks up cleanly.
        starts = [0] + [m.end() for m in re.finditer(r"[.!?]\s+", text)]
        start = max(i for i in starts if i <= upto) if upto < len(text) else len(text)
        self.hub.conv.interrupted = text[start:].strip()

    async def stop_speaking(self) -> None:
        self._where_we_got_to()
        # Stop the sound, the answer still being written, and the voice still being made.
        for task in (self.speak_task, self.interim_task, self.reply_task):
            if task and not task.done():
                task.cancel()
        await self.send_json({"type": "stop_audio", "seq": self.seq})
        self.seq = (self.seq + 1) % 256  # any frames still in flight now carry a stale seq
        await self.set_state("idle")

    # ---------- control messages ----------
    async def on_message(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            self.info = {k: v for k, v in msg.items() if k != "type"}
            await self.hub.greet(self)
        elif t == "stats":
            self.info.update({k: v for k, v in msg.items() if k != "type"})
            await self.hub.broadcast_ui({"type": "event", "kind": "device", "session": self.id, "info": self.info})
        elif t == "playback_done":
            if msg.get("seq") in (None, self.seq):
                self.playback_done.set()
        elif t == "audio_mode":
            self.hands_free = bool(msg.get("on"))
            if not self.hands_free and self.state == "listening" and not self.ptt:
                self.state = "idle"
        elif t == "ptt_start" or (t == "button" and self.state in ("idle",)):
            if self.state == "speaking":
                await self.stop_speaking()
            await self.start_listening(earcon=True, ptt=(t == "ptt_start"), no_speech_s=8.0)
        elif t == "ptt_end":
            await self.end_ptt()
        elif t == "button":
            if self.state == "speaking":
                await self.stop_speaking()
        elif t == "mic_test":
            await self.hub.mic_test(self, bool(msg.get("on")), msg.get("target") or "me", msg.get("enroll") or "")
        elif t == "text":
            text = (msg.get("text") or "").strip()
            if text:
                if self.state == "speaking":
                    await self.stop_speaking()
                asyncio.create_task(self.hub.process_text(self, text))
        elif t == "stop_speaking":
            await self.stop_speaking()
        elif t == "alarm_fired":
            await self.hub.alarm_fired(self, msg.get("id"), msg.get("kind"))
        elif t == "alarm_stopped":
            await self.hub.alarm_stopped(self, msg.get("id"), msg.get("reason", "button"))


class Hub:
    def __init__(self, settings: Settings, http=None, claude_http=None, stt=None):
        self.settings = settings
        d = settings.data_dir
        self.llm = ClaudeLLM(settings, claude_http)  # Buddy's brain
        self.listener = GroqLLM(settings, http)  # optional: judges whether room talk is for Buddy
        self.judge = ContextJudge(settings, self.listener)
        self.stt = stt or LocalSTT(settings)
        self.voiceid = VoiceID(settings.data_dir)  # knows who is speaking
        self.asked_who: dict | None = None  # asked a new voice "who's this?": their held answer and voice
        self.greeted: dict[str, float] = {}  # name -> when Buddy last said hello to them
        self.heard: dict[str, float] = {}  # name -> when Buddy last heard their voice
        self.awake = True  # "off" sets this to False until the next "hey buddy"
        self.device_gone_at: dict[str, float] = {}
        self.ring_started: dict = {}  # alarm id -> when it started ringing
        self.timed_out: set = set()  # alarms this server stopped because nobody answered  # desk unit name -> when it last disconnected
        self.last_spoke_at = 0.0
        self.tts = TTSRouter(settings, ElevenLabsTTS(settings, http))
        self.weather = Weather(settings, http)
        self.alarms = AlarmStore(d / "alarms.json", settings["timezone"])
        self.memory = MemoryStore(d / "memory.json")
        self.memory_mgr = MemoryManager(self.memory, self.llm, settings)
        self.brain = Brain(settings, self.llm, self.alarms, self.memory, self.memory_mgr, self.weather)
        self.spotify = Spotify(settings)
        self.brain.spotify = self.spotify
        self.music = MusicPlayer(settings, self)  # Spotify through Buddy's own speaker
        self.spotify.music = self.music
        self.sounds = AlarmSounds(settings)  # your own alarm sounds
        self.study = StudyStore(d)  # Study Buddy: lessons, notes and questions per person
        self.study_worker = StudyWorker(self.study, self.stt, self.llm)
        self.study_worker.cloud = self.study_cloud
        self.study_worker.teachers = VoiceID(d / "study" / "teachers", share=self.voiceid)  # teachers' voices
        from .practice import Practice
        self.practice = Practice(self.study.dir)  # "Start revising": questions that fit each person
        self.brain.study = (self.study, self.practice)  # so Buddy can say how revising is going
        self.brain.study_cloud = self.study_cloud
        self.brain.sounds = self.sounds
        self.conv = Conversation()
        self.sessions: dict[str, Session] = {}
        self.fired_cache: dict[str, Alarm] = {}
        self.ui_fired: set[tuple[str, int]] = set()
        self.log: collections.deque = collections.deque(maxlen=200)
        self.tasks: list[asyncio.Task] = []
        self.alarms.on_change(lambda: self._soon(self.push_schedule()))
        self.memory.on_change(lambda: self._soon(self.broadcast_ui({"type": "event", "kind": "memory"})))
        settings.on_change(self._settings_changed)

    def _soon(self, coro) -> None:
        try:
            asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()

    def _settings_changed(self, changes: dict) -> None:
        if "timezone" in changes:
            self.alarms.set_timezone(changes["timezone"])
        if {"music_on_buddy", "assistant_name"} & changes.keys():
            self._soon(self.music.restart())
        if "volume" in changes:
            self.brain.volume = int(changes["volume"])
            self._soon(self.broadcast({"type": "volume", "level": int(changes["volume"])}, role="device"))
        if {"strip_leds", "strip_brightness"} & changes.keys():
            self._soon(self.broadcast({"type": "strip", "leds": int(self.settings["strip_leds"]),
                                       "brightness": int(self.settings["strip_brightness"])}, role="device"))
        if "device_mic_shift" in changes:
            self._soon(self.broadcast({"type": "mic_gain", "shift": int(changes["device_mic_shift"])}, role="device"))
        self._soon(self.broadcast_ui({"type": "event", "kind": "settings"}))

    # ---------- lifecycle ----------
    async def start(self) -> None:
        asyncio.create_task(self.music.start())
        self.tasks.append(asyncio.create_task(self._scheduler()))
        self.tasks.append(asyncio.create_task(self._housekeeping()))
        asyncio.create_task(self.listener.discover())
        if hasattr(self.stt, "warm"):
            asyncio.create_task(asyncio.to_thread(self.stt.warm))
        asyncio.create_task(asyncio.to_thread(self.voiceid.warm))
        asyncio.create_task(self.tts.eleven.refresh_quota())
        asyncio.create_task(self.weather.fetch())
        self.study_worker.start()

    def study_cloud(self) -> tuple[str, str] | None:
        """(Study Buddy website, sync key) once Buddy is connected to it, else None."""
        key = self.settings.secret("STUDY_SYNC_KEY")
        url = (self.settings["study_cloud_url"] or "").strip()
        return (url, key) if key and url.startswith("http") else None

    async def stop(self) -> None:
        await self.music.stop()
        for t in self.tasks:
            t.cancel()

    def add(self, s: Session) -> None:
        self.sessions[s.id] = s

    def remove(self, s: Session) -> None:
        self.sessions.pop(s.id, None)
        for x in self.sessions.values():  # a closed Mic test page stops its test
            if x.mic_test_viewer is s:
                x.mic_test, x.mic_test_viewer = False, None
        for t in (s.worker, s.speak_task):
            if t and not t.done():
                t.cancel()
        s._drop_live()

    def devices(self) -> list[Session]:
        return [s for s in self.sessions.values() if s.role == "device"]

    async def broadcast(self, msg: dict, role: str | None = None, exclude: Session | None = None) -> None:
        for s in list(self.sessions.values()):
            if s is not exclude and (role is None or s.role == role):
                await s.send_json(msg)

    async def broadcast_ui(self, msg: dict, exclude: Session | None = None) -> None:
        await self.broadcast(msg, role="ui", exclude=exclude)

    def record(self, event: str, **data) -> None:
        entry = {"t": time.time(), "event": event, **data}
        self.log.append(entry)
        self._soon(self.broadcast_ui({"type": "event", "kind": "log", "entry": entry}))

    async def greet(self, s: Session) -> None:
        await s.send_json({"type": "hello_ack", "server_time": int(time.time()), "volume": self.brain.volume,
                           "name": self.settings["assistant_name"], "session": s.id,
                           "mic_shift": int(self.settings["device_mic_shift"]),
                           "strip_leds": int(self.settings["strip_leds"]),
                           "strip_brightness": int(self.settings["strip_brightness"])})
        if s.role == "device":
            await self.push_schedule(s)
            if self.just_switched_on(s):
                await s.play_startup_sound()
        self.record("connect", session=s.id, role=s.role, info=s.info)

    async def mic_test(self, viewer: Session, on: bool, target: str, enroll: str = "") -> None:
        """Start or stop the live mic test. target: "me" (this browser) or a desk unit's session id.
        enroll: a name, to add everything said to that person's voice (the Your voice tab)."""
        for x in self.sessions.values():  # only one test at a time, per viewer
            if x.mic_test_viewer is viewer:
                x.mic_test, x.mic_test_viewer, x.enroll_name = False, None, ""
        if not on:
            return
        who = viewer if target == "me" else self.sessions.get(target)
        if who is None:
            await viewer.send_json({"type": "event", "kind": "mic_test", "error": "That mic isn't connected."})
            return
        who.mic_test, who.mic_test_viewer = True, viewer
        who.enroll_name = " ".join(str(enroll or "").split())[:40]
        who.mic_test_until = time.time() + 300  # switches itself off after 5 minutes
        who.mt_buf, who.mt_silence, who.mt_peak, who.mt_clipped = [], 0, 0.0, 0
        self.record("mic_test", target=who.id)

    def just_switched_on(self, s: Session) -> bool:
        """Plugged in, rather than a quick Wi-Fi reconnect. Newer board software says how long
        it's been on; older software doesn't, so then it counts as on if it was gone a minute."""
        uptime = s.info.get("uptime")
        if uptime is not None:
            return float(uptime) < 60
        gone = self.device_gone_at.get(s.name)
        return gone is None or time.time() - gone > 60

    def device_left(self, s: Session) -> None:
        if s.role == "device":
            self.device_gone_at[s.name] = time.time()

    async def push_schedule(self, only: Session | None = None) -> None:
        msg = {"type": "alarms", "server_time": int(time.time()), "fires": self.alarms.device_schedule()}
        for s in ([only] if only else self.devices()):
            await s.send_json(msg)
        if not only:
            await self.broadcast_ui({"type": "event", "kind": "alarms"})

    # ---------- the pipeline ----------
    def room_listening_active(self) -> bool:
        return self.awake and bool(self.settings["room_listening_on"]) and self.stt.available()

    def set_awake(self, on: bool) -> None:
        if on != self.awake:
            self.awake = on
            self.record("awake" if on else "asleep")
            self._soon(self.broadcast_ui({"type": "event", "kind": "settings"}))

    async def _learn_voice(self, who: dict) -> None:
        """Learning as people talk: someone still being learnt gets every sentence added until Buddy has
        enough to name them; someone it knows well gets clear matches added, so it keeps up with their
        voice (a cold, a new mic...). Also notes who's around."""
        if not who:
            return
        name = who.get("name") or who.get("learning")
        if name:
            self.heard[name] = time.time()
        v = who.get("print")
        if v is None or not self.settings["voice_learn_on"]:
            return
        thr = float(self.settings["voice_id_threshold"])
        if who.get("learning") or (who.get("name") and who.get("score", 0) >= thr + 0.15):
            was = self.voiceid.trusted(name, float(self.settings["voice_trust_seconds"]))
            await asyncio.to_thread(self.voiceid.add_print, name, v, float(who.get("seconds", 0)))
            if not was and self.voiceid.trusted(name, float(self.settings["voice_trust_seconds"])):
                self.record("voice_now_known", name=name)

    def present(self, minutes: float = 15) -> list[str]:
        """People whose voices Buddy heard in the last few minutes."""
        cutoff = time.time() - minutes * 60
        return [n for n, t in sorted(self.heard.items(), key=lambda x: -x[1]) if t >= cutoff]

    async def who_spoke(self, audio: np.ndarray) -> dict:
        """{"name": ..., "score": ...} for whoever said this ("" if unknown); {} when voice ID is off."""
        if not self.settings["voice_id_on"] or not self.voiceid.people or not self.voiceid.installed():
            return {}
        try:
            return await asyncio.to_thread(self.voiceid.identify, audio, float(self.settings["voice_id_threshold"]),
                                           float(self.settings["voice_trust_seconds"]))
        except Exception as e:  # noqa: BLE001 - never let this stop an answer
            log.info("voice ID skipped: %s", e)
            return {}

    async def process_audio(self, s: Session, audio: np.ndarray, speech_s: float, silent: str | None = None,
                            mentioned: bool = False, timer: "timing.TurnTimer | None" = None,
                            early: "asyncio.Task | None" = None) -> None:
        if timer:
            timing.current.set(timer)  # every stage of this turn marks its time on it
        t0 = time.monotonic()
        who_task = asyncio.create_task(self.who_spoke(audio))  # alongside the transcribing: no extra wait
        try:
            try:
                # Started at the first pause (and they didn't carry on): usually done already.
                text = await early if early is not None else await self.stt.transcribe(audio, "", speech_s)
                if early is not None:
                    timing.note("early_transcript", True)
            except asyncio.CancelledError:
                text = await self.stt.transcribe(audio, "", speech_s)
        except STTError as e:
            log.warning("STT failed: %s", e)
            self.record("error", where="stt", error=str(e))
            if silent:
                await s.set_state("idle")
                return
            await self.announce(s, str(e) if "Update Buddy" in str(e) else "Hmm, I didn't quite get that. Say it again for me?")
            return
        stt_ms = int((time.monotonic() - t0) * 1000)
        timing.mark("stt_done")
        s.speaker = await who_task
        s.turn_audio = audio
        await self._learn_voice(s.speaker)
        if s.interrupted_text and time.time() - s.interrupted_at < 20 and text.strip():
            cleaned = intents.strip_echo(text, s.interrupted_text)
            if cleaned != text:
                self.record("echo_removed", heard=text, kept=cleaned)
                text = cleaned
            s.interrupted_text = ""
        # How loud it was and what came out: shows a too-quiet (or clipping) mic in the Activity tab.
        if len(audio):
            peak = float(np.abs(audio.astype(np.int32)).max())
            self.record("capture", session=s.id, heard=text, speech_s=round(speech_s, 2),
                        loudest_db=round(20 * np.log10(max(peak, 1.0) / 32768), 1),
                        level_db=round(rms_dbfs(audio), 1), clipped=bool(peak >= 32000), mode=silent or "normal",
                        **({"speaker": s.speaker.get("name") or (f"{s.speaker['learning']} (still learning)"
                                                                 if s.speaker.get("learning") else "unknown"),
                            "voice_score": s.speaker.get("score")} if s.speaker else {}))
        if silent == "wake":
            # A soft "hey buddy" out of nowhere. If the words are clearly something else, ignore it;
            # if they're unclear, wake up anyway to be sure.
            words = text.lower().split()
            starts_buddy = intents.wake_heard(text) or any(w.startswith(("budd", "bod", "bud")) for w in words[:5])
            # Only ignore it when the words are clearly something else; unclear or short: wake to be sure.
            if len(words) > 3 and not starts_buddy:
                self.record("wake_rejected", session=s.id, heard=text)
                await self.note(s, f'Heard "{text}", but it didn\'t sound like "hey buddy".')
                await s.set_state("idle")
                return
            self.record("wake_verified", session=s.id, heard=text)
            await self._accept(s, text, stt_ms)
            return
        if silent in ("context", "room"):
            if not text.strip():
                await s.set_state("idle")
                return
            self.judge.hear(text)
            since = time.time() - self.last_spoke_at if self.last_spoke_at else 1e9
            for_buddy = await self.judge.is_for_buddy(text, mentioned, self.conv.last_reply, since)
            self.record("heard", session=s.id, heard=text, for_buddy=for_buddy, how=self.judge.last_how,
                        stt_ms=stt_ms)
            if not for_buddy:
                await self.note(s, f'Heard "{text}" and decided it wasn\'t for me.')
                await s.set_state("idle")
                return
            await self._accept(s, text, stt_ms)
            return
        if not text.strip() and speech_s >= 0.3:
            text = await self._second_listen(audio, speech_s)  # louder, trimmed: often enough
        if not text.strip():
            self.record("nothing_heard", session=s.id, speech_s=round(speech_s, 2))
            if speech_s >= 0.3:
                await self.note(s, "Heard you talking but couldn't make out any words"
                                   + (", so I'm still listening." if speech_s >= 0.6 and not s.relistened else "."))
            if speech_s >= 0.6 and not s.relistened:
                # Someone said something we couldn't make out: no apology, just keep listening
                # (the light stays on) so they can say it again without "hey buddy".
                s.relistened = True
                await s.start_listening(earcon=False, no_speech_s=6.0)
                return
            s.relistened = False
            await s.earcon("nospeech")
            await s.set_state("idle")
            return
        s.relistened = False
        if not intents.strip_wake(text):  # just "hey buddy" and a pause: wait for the question
            await s.start_listening(earcon=False, no_speech_s=6.0)
            return
        await self.respond(s, text, stt_ms=stt_ms)

    async def _second_listen(self, audio: np.ndarray, speech_s: float) -> str:
        """Speech was heard but no words came out: try again with the quiet start and end trimmed
        off and the level brought up, which is often all the ears needed."""
        x = audio.astype(np.float64)
        frame = SR // 50  # 20 ms
        n = len(x) // frame
        if n < 5:
            return ""
        rms = np.sqrt(np.mean(x[: n * frame].reshape(n, frame) ** 2, axis=1))
        loud = np.where(rms > max(30.0, rms.max() * 0.08))[0]
        if not len(loud):
            return ""
        a, b = max(0, loud[0] - 10) * frame, min(n, loud[-1] + 15) * frame  # keep 0.2 s before, 0.3 s after
        x = x[a:b]
        level = np.sqrt(np.mean(x ** 2)) or 1.0
        x = np.clip(x * min(8.0, (32768 * 10 ** (-20 / 20)) / level), -32000, 32000).astype(np.int16)
        try:
            text = await self.stt.transcribe(x, "", max(speech_s, 0.5))
        except STTError:
            return ""
        if text.strip():
            self.record("heard_on_second_try", heard=text)
        return text

    async def _accept(self, s: Session, text: str, stt_ms: int = 0) -> None:
        """Something heard silently turned out to be for Buddy: light up and answer it."""
        self.set_awake(True)
        await s.wake_sound()
        await s.earcon("wake")
        await s.set_state("thinking")
        if not intents.strip_wake(text):  # just "hey buddy": now listen for the question
            await s.start_listening(earcon=False, no_speech_s=6.0)
            return
        await self.respond(s, text, stt_ms=stt_ms)

    async def process_text(self, s: Session, text: str) -> None:
        await s.set_state("thinking")
        await self.respond(s, text)

    async def note(self, s: Session, text: str) -> None:
        """A small grey line in the chat explaining why Buddy went quiet (not spoken)."""
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "note", "text": text})

    async def announce(self, s: Session, text: str) -> None:
        """Speak something that isn't a reply to what was just said, and show it in the chat."""
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant", "text": text})
        await s.speak(text)

    async def respond(self, s: Session, text: str, stt_ms: int = 0) -> None:
        s.turn_running += 1
        timer = timing.current.get()
        if timer:
            timer.text = text
        try:
            await self._respond(s, text, stt_ms)
        finally:
            s.turn_running -= 1
            self.finish_timing(timer)  # if Buddy didn't speak (it's already done if it did)

    def finish_timing(self, timer: "timing.TurnTimer | None") -> None:
        if timer and self.settings["timing_log"]:
            timer.finish(self.settings.data_dir, self.record)

    async def _respond(self, s: Session, text: str, stt_ms: int = 0) -> None:
        speaker, s.speaker = s.speaker, {}
        audio, s.turn_audio = s.turn_audio, None
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "user", "text": text,
                                 **({"speaker": speaker.get("name", "")} if speaker else {})})
        if await self._answer_to_who(s, text, audio):
            return
        starting = not self.conv.history or time.time() - self.conv.last_active > 600
        ctx = {"ringing": s.ringing or next((d.ringing for d in self.devices() if d.ringing), None),
               "say": s.interim, "present": self.present(),
               # Who's talking: their name, or "unknown voice" when it heard someone it doesn't know yet.
               "speaker": speaker.get("name") or ("unknown voice" if speaker.get("score", 0) > 0 else "")}
        speech, speaking = None, None
        if self._can_stream(s, starting, speaker):
            # Speak Claude's answer while it's being written (speech.py).
            speech = SpeechStream(self, timing.current.get(), live=await s.take_live())
            name = speaker.get("name", "")
            if (starting and name and self.settings["greet_by_voice"]
                    and time.time() - self.greeted.get(name, 0) > 3600):
                self.greeted[name] = time.time()
                speech.add_text(random.choice([f"Hey {name}!", f"Hi {name}!", f"Alright {name}?"]))
            ctx["on_text"] = speech.feed
            speaking = asyncio.create_task(s.speak_stream(speech))
        work = asyncio.create_task(self.brain.handle(text, self.conv, ctx))
        s.reply_task = work
        try:
            # Still nothing after a few seconds (and no "one sec" yet)? Say so, rather than leave a silence.
            done, _ = await asyncio.wait({work}, timeout=3.5)
            if not done and not ctx.get("said_while") and not (speech and speech.started):
                ctx["said_while"] = True
                await s.interim(random.choice(STILL_WORKING))
            reply: Reply = await work
        except Exception as e:  # noqa: BLE001
            log.exception("brain failed")
            reply = Reply("Ah, sorry, something went a bit wrong on my end. Give it another go?", intent="error")
            self.record("error", where="brain", error=str(e))
        if speech is not None:
            if s.paused is not None and not speech.played:
                # An alarm broke in before any of the answer was heard: hold it, as before streaming.
                speaking.cancel()
                try:
                    await speaking
                except asyncio.CancelledError:
                    pass
                ctx.pop("check_later", None)
                if reply.streamed and self.settings["fact_check"] and reply.intent in ("llm", "llm_deep"):
                    reply = await self.brain.check_after(text, self.conv, ctx, reply.text) or reply
            else:
                await self._finish_streamed(s, text, reply, speech, speaking, ctx, stt_ms)
                return
        s.turns += 1
        s.last_intent = reply.intent or ""
        if s.ringing and s.paused is not None and reply.intent not in ("stop", "off", "snooze"):
            # An alarm went off while this was being worked out: keep it for when the alarm stops.
            for act in reply.actions:
                await self.apply_action(s, act)
            s.paused = {"what": "held", "text": reply.text, "follow_up": reply.follow_up}
            await s.set_state("idle")
            self.record("turn", session=s.id, heard=text, said=reply.text, intent=reply.intent, model=reply.model,
                        stt_ms=stt_ms, brain_ms=reply.ms, held_for_alarm=True)
            return
        if (s.paused is not None and s.paused["what"] == "thinking" and reply.text
                and reply.intent not in ("stop", "off", "snooze")):
            # An alarm came and went while this was being worked out: say sorry, then answer.
            s.paused = None
            reply.text = random.choice(SORRY) + " " + reply.text
        for act in reply.actions:
            await self.apply_action(s, act)
        if starting and reply.text and reply.intent not in QUIET_INTENTS and self.settings["greet_by_voice"]:
            self._greet(reply, speaker, audio)
        self.record("turn", session=s.id, heard=text, said=reply.text, intent=reply.intent, model=reply.model,
                    stt_ms=stt_ms, brain_ms=reply.ms)
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant",
                                 "text": reply.text, "model": reply.model, "intent": reply.intent})
        if reply.text:
            await s.speak(reply.text, then_listen=reply.follow_up)
        else:
            if reply.intent in ("off", "stop"):
                await self.note(s, f'Went quiet: I took "{text}" as {"off" if reply.intent == "off" else "stop"}.')
            elif reply.intent not in ("music",):
                await self.note(s, f'Heard "{text}" but had nothing to say back.')
            await s.set_state("idle")

    def _can_stream(self, s: Session, starting: bool, speaker: dict) -> bool:
        """Stream this answer? Not when it has to be held back: a new voice is asked "who's this?" first,
        an alarm is going, or a missed alarm has to be worked into the answer."""
        if not self.settings["stream_replies"] or s.ringing or s.paused is not None or self.brain.missed:
            return False
        unknown = (speaker and not speaker.get("name") and not speaker.get("learning")
                   and speaker.get("score", 0) > 0)
        return not (starting and unknown and self.settings["greet_by_voice"])

    async def _finish_streamed(self, s: Session, text: str, reply: Reply, speech, speaking, ctx: dict,
                               stt_ms: int) -> None:
        """The brain is done and the answer may already be half spoken: add anything that wasn't
        streamed, let the speaking finish, then check facts (and correct, if needed)."""
        s.turns += 1
        s.last_intent = reply.intent or ""
        for act in reply.actions:
            await self.apply_action(s, act)
        if reply.text and not reply.streamed:
            speech.add_text(reply.text)  # a quick command, or a confirmation: said in one go
        check = None
        if ctx.get("check_later") and reply.text:
            check = asyncio.create_task(self.brain.check_after(text, self.conv, ctx, reply.text))
        # Keep listening afterwards only if it asked something and nothing needs correcting.
        follow = reply.follow_up
        speech.then_listen = follow and check is None
        speech.end()
        said = speech.text or reply.text
        self.record("turn", session=s.id, heard=text, said=said, intent=reply.intent, model=reply.model,
                    stt_ms=stt_ms, brain_ms=reply.ms, streamed=reply.streamed)
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant",
                                 "text": said, "model": reply.model, "intent": reply.intent})
        await speaking
        if not speech.n_sentences:
            if reply.intent in ("off", "stop"):
                await self.note(s, f'Went quiet: I took "{text}" as {"off" if reply.intent == "off" else "stop"}.')
            elif reply.intent not in ("music",):
                await self.note(s, f'Heard "{text}" but had nothing to say back.')
            await s.set_state("idle")
        if check is not None:
            try:
                fix = await check
            except Exception:  # noqa: BLE001 - a failed check mustn't break anything
                log.exception("fact-check after speaking failed")
                fix = None
            if fix:
                self.record("corrected", heard=text, said=fix.text)
                await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id,
                                         "role": "assistant", "text": fix.text, "intent": "correction"})
                await s.speak(fix.text, then_listen=False)
            elif follow and s.hands_free and s.state == "idle":
                await s.start_listening(earcon=True, no_speech_s=5.0)

    def _greet(self, reply: Reply, speaker: dict, audio: np.ndarray | None) -> None:
        """Start of a conversation. Someone it knows: "Hey Wells!" (at most once an hour each). A voice it
        doesn't know: hold the answer and ask "Oh, hello! Who's this?" first."""
        name = speaker.get("name", "")
        if speaker.get("learning"):
            return  # already asked who they are; still learning their voice
        if name:
            if time.time() - self.greeted.get(name, 0) > 3600:
                self.greeted[name] = time.time()
                reply.text = random.choice([f"Hey {name}!", f"Hi {name}!", f"Alright {name}?"]) + " " + reply.text
            return
        # Only ask when it really heard a voice (score > 0) and it wasn't close to anyone saved.
        if speaker and speaker.get("score", 0) > 0 and audio is not None:
            self.asked_who = {"text": reply.text, "follow_up": reply.follow_up, "audio": audio, "at": time.time(),
                              "best": speaker.get("best", ""), "score": speaker.get("score", 0)}
            reply.text, reply.follow_up = "Oh, hello! Who's this?", True
            self.record("asked_who", score=speaker.get("score"), closest=speaker.get("best"))

    async def _answer_to_who(self, s: Session, text: str, audio: np.ndarray | None) -> bool:
        """The reply to "who's this?": remember their voice under that name, then give the answer they were
        waiting for. Returns False if it wasn't a name (then it's treated as a new request)."""
        asked, self.asked_who = self.asked_who, None
        if not asked or time.time() - asked["at"] > 90:
            return False
        name = intents.name_answer(text)
        if not name:
            return False
        known = name.lower() in {n.lower() for n in self.voiceid.people}
        if known:
            name = next(n for n in self.voiceid.people if n.lower() == name.lower())
        learnt = 0.0
        for pcm in (asked["audio"], audio):
            if pcm is None:
                continue
            try:
                r = await asyncio.to_thread(self.voiceid.add_audio, name, pcm, "talking to Buddy")
                learnt += r["seconds"]
            except VoiceIDError:
                pass  # too short to learn from
        self.record("voice_learnt", name=name, seconds=round(learnt, 1), already_known=known)
        self.greeted[name] = time.time()
        hello = (f"Oh, sorry {name}, I didn't recognise you!" if known else f"Nice to meet you, {name}!")
        if not learnt and not known:
            hello += " I'll learn your voice as we chat."
        said = " ".join(x for x in (hello, asked["text"]) if x)
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant", "text": said})
        await s.speak(said, then_listen=asked["follow_up"])
        return True

    async def apply_action(self, s: Session, act: dict) -> None:
        kind = act.get("type")
        if kind == "sleep":
            self.set_awake(False)
        elif kind == "volume":
            await self.broadcast({"type": "volume", "level": act["level"]})
        elif kind == "stop":
            if s.state == "speaking":
                await s.stop_speaking()
        elif kind == "alarm_stop":
            targets = [x for x in self.sessions.values() if x.ringing] or self.devices()
            for x in targets:
                await x.send_json({"type": "alarm_stop", "quiet": act.get("quiet", False)})
                x.ringing = None

    # ---------- alarms ----------
    def _resolve(self, alarm_id: str | None) -> Alarm | None:
        if not alarm_id:
            return None
        return self.alarms.get(alarm_id) or self.fired_cache.get(alarm_id)

    async def alarm_fired(self, s: Session, alarm_id: str | None, kind: str | None) -> None:
        a = self._resolve(alarm_id)
        if a:
            self.fired_cache[a.id] = a
            self.alarms.fired(a.id)
        kind = (a.kind if a else kind) or "alarm"
        self.record("alarm_fired", session=s.id, id=alarm_id, kind=kind, label=a.label if a else "")
        if kind == "reminder":
            busy = await self.pause_for_alarm(s)
            await asyncio.sleep(1.6)  # let the chime finish first
            text = f"Here's your reminder: {a.label}." if a and a.label else "This is your reminder."
            await self.announce(s, text)
            if busy:
                self._soon(self._resume_after_alarm(s, s.turns))
        else:
            await self.pause_for_alarm(s)
            s.reset_ring_ears()
            s.ringing = alarm_id
            await self.broadcast_ui({"type": "event", "kind": "ringing", "session": s.id, "id": alarm_id,
                                     "label": a.label if a else "", "alarm_kind": kind})
            number = (a.sound or int(self.settings["default_alarm_sound"])) if a and kind == "alarm" else 0
            if number:
                self._soon(self._ring_with_sound(s, alarm_id, number))
            if kind == "alarm":
                self.ring_started[alarm_id] = datetime.now(ZoneInfo(self.settings["timezone"]))
                self._soon(self._give_up_after(s, alarm_id, 30))

    async def _give_up_after(self, s: Session, alarm_id: str | None, seconds: float) -> None:
        """Nobody answered the alarm: stop it (older board software rings for 10 minutes)."""
        await asyncio.sleep(seconds)
        if s.ringing == alarm_id:
            self.timed_out.add(alarm_id)
            await s.send_json({"type": "alarm_stop", "reason": "timeout"})

    async def _ring_with_sound(self, s: Session, alarm_id: str | None, number: int) -> None:
        """Play your alarm sound on a loop while the alarm rings. The board stops its own beeps while
        this sound arrives, and goes back to them if it stops (the laptop went to sleep, say)."""
        pcm = await asyncio.to_thread(self.sounds.pcm, number)
        if not pcm:
            return
        started = time.monotonic()
        while s.ringing == alarm_id and time.monotonic() - started < 40:
            s.seq = (s.seq + 1) % 256
            seq = s.seq
            await s.send_json({"type": "audio_start", "seq": seq, "rate": SR, "text": "", "ring": True})
            t0, sent = time.monotonic(), 0
            for i in range(0, len(pcm), 3200):
                if s.ringing != alarm_id:
                    break
                ahead = sent / BYTES_PER_S - (time.monotonic() - t0)
                if ahead > 0.6:
                    await asyncio.sleep(ahead - 0.6)
                await s.send_audio(seq, pcm[i:i + 3200])
                sent += 3200
            await s.send_json({"type": "audio_end", "seq": seq, "bytes": sent})
            await asyncio.sleep(max(0.0, sent / BYTES_PER_S - (time.monotonic() - t0)) + 0.4)
        if s.ringing != alarm_id:
            await s.send_json({"type": "stop_audio", "seq": s.seq})

    async def pause_for_alarm(self, s: Session) -> bool:
        """An alarm is going off: stop talking (or listening) and remember where the conversation was,
        to carry on once it's stopped. Returns True if there was a conversation to pause."""
        if s.paused is not None:
            return True
        if s.state == "speaking" and s.speaking_text and not (s.interim_task and not s.interim_task.done()):
            await s.stop_speaking()  # notes how far it got in conv.interrupted
            s.paused = {"what": "speaking", "text": self.conv.interrupted}
            self.conv.interrupted = ""
        elif s.state == "listening" and not s.silent:
            s.capture, s.ptt = [], False
            await s.set_state("idle")
            s.paused = {"what": "listening"}
        elif s.turn_running or s.state == "thinking":
            if s.interim_task and not s.interim_task.done():
                s.interim_task.cancel()
                await s.send_json({"type": "stop_audio", "seq": s.seq})
                s.seq = (s.seq + 1) % 256
            s.paused = {"what": "thinking"}
        else:
            return False
        self.record("paused_for_alarm", session=s.id, what=s.paused["what"])
        return True

    async def _resume_after_alarm(self, s: Session, turns_then: int, prefix: str = "") -> None:
        """The alarm has stopped: pick the conversation back up, unless they've moved on."""
        for _ in range(150):  # let a "stop"/"snooze" exchange finish first (up to 30 s)
            if s.ringing:
                return  # another alarm: it will resume after that one
            if s.state not in ("listening", "thinking", "speaking") and not s.turn_running:
                break
            await asyncio.sleep(0.2)
        p, s.paused = s.paused, None
        if not p:
            return
        moved_on = s.turns > turns_then and s.last_intent not in ("stop", "snooze", "thanks", "")
        if moved_on or s.last_intent == "off":
            if p["what"] in ("speaking", "held") and p.get("text"):
                self.conv.interrupted = p["text"]  # still there for "carry on"
            return
        sorry = random.choice(SORRY)
        follow_up = False
        if p["what"] == "speaking" and p.get("text"):
            text = random.choice(AS_I_WAS_SAYING) + " " + p["text"]
        elif p["what"] == "held" and p.get("text"):
            text, follow_up = sorry + " " + p["text"], bool(p.get("follow_up"))
        elif p["what"] == "listening":
            text, follow_up = random.choice(YOU_WERE_SAYING), True
        else:
            text = ""
        text = " ".join(x for x in (prefix, text) if x)
        if not text:
            return
        self.record("resumed_after_alarm", session=s.id, what=p["what"])
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant", "text": text})
        await s.speak(text, then_listen=follow_up)

    async def ring_heard(self, s: Session, audio: np.ndarray) -> None:
        """Something said while the alarm rang (no "hey buddy" needed): act on "stop" or "snooze"."""
        try:
            text = await self.stt.transcribe(audio, "", len(audio) / SR)
        except STTError:
            return
        finally:
            s.ring_busy = False
        cmd = intents.ring_command(text)
        if text.strip():
            self.record("heard_while_ringing", session=s.id, heard=text, command=cmd[0] if cmd else None)
        if not cmd or not s.ringing:
            return
        alarm_id = s.ringing
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "user", "text": text})
        if cmd[0] == "stop":
            await self.apply_action(s, {"type": "alarm_stop"})
            return
        minutes = cmd[1] or int(self.settings["snooze_minutes"])
        a = self._resolve(alarm_id)
        if a:
            self.alarms.snooze(a, minutes)
        line = f"Okay, snoozing for {minutes} minutes."
        if s.paused is not None:
            s.paused["prefix"] = line  # said together with picking the conversation back up
            await self.apply_action(s, {"type": "alarm_stop", "quiet": True})
        else:
            await self.apply_action(s, {"type": "alarm_stop", "quiet": True})
            await self.announce(s, line)

    async def alarm_stopped(self, s: Session, alarm_id: str | None, reason: str) -> None:
        a = self._resolve(alarm_id)
        s.ringing = None
        s.reset_ring_ears()
        if s.paused is not None:
            # It broke into a conversation: no briefing, just carry on where we were.
            rang_at = self.ring_started.pop(alarm_id, None)
            self.timed_out.discard(alarm_id)
            self.record("alarm_stopped", session=s.id, id=alarm_id, reason=reason)
            await self.broadcast_ui({"type": "event", "kind": "ringing", "session": s.id, "id": None})
            self._soon(self._resume_after_alarm(s, s.turns, s.paused.pop("prefix", "")))
            return
        rang_at = self.ring_started.pop(alarm_id, None)
        if a and a.kind == "alarm" and (reason == "timeout" or alarm_id in self.timed_out):
            self.timed_out.discard(alarm_id)
            when = rang_at or datetime.now(ZoneInfo(self.settings["timezone"]))
            self.brain.missed.append(f"{spoken_time(when)}" + (f" ({a.label})" if a.label else ""))
            self.record("alarm_missed", session=s.id, id=alarm_id, at=spoken_time(when))
            await self.broadcast_ui({"type": "event", "kind": "ringing", "session": s.id, "id": None})
            return  # no briefing: nobody's there to hear it
        self.record("alarm_stopped", session=s.id, id=alarm_id, reason=reason)
        await self.broadcast_ui({"type": "event", "kind": "ringing", "session": s.id, "id": None})
        if not a or a.kind != "alarm" or reason == "snooze":
            return
        now = datetime.now(ZoneInfo(self.settings["timezone"]))
        if self.settings["morning_briefing"] and 4 <= now.hour < 12:
            await self.announce(s, await self.morning_briefing(a, now))
        elif a.label:
            await self.announce(s, f"That was your {a.label} alarm.")

    async def morning_briefing(self, a: Alarm, now: datetime) -> str:
        name = self.settings["user_name"]
        parts = [f"Good morning{', ' + name if name else ''}. It's {spoken_time(now)} on {DAY_NAMES[now.weekday()]}."]
        if a.label:
            parts.append(f"That was your {a.label} alarm.")
        parts.append(await self.weather.spoken("today"))
        end_of_day = now.replace(hour=23, minute=59).timestamp()
        todays = [x for ts, x in self.alarms.upcoming(time.time(), horizon_s=end_of_day - time.time())
                  if x.kind == "reminder"]
        if todays:
            r = todays[0]
            parts.append(f"You've got a reminder later to {r.label}.")
        return " ".join(parts)

    async def _scheduler(self) -> None:
        """Rings alarms in web pages, and keeps bookkeeping right when no device is online."""
        while True:
            try:
                await asyncio.sleep(1.0)
                now = time.time()
                for ts, a in self.alarms.upcoming(now - 2.0, horizon_s=3.0):
                    key = (a.id, int(ts))
                    if ts > now or key in self.ui_fired:
                        continue
                    self.ui_fired.add(key)
                    self.fired_cache[a.id] = a
                    for ui in [x for x in self.sessions.values() if x.role == "ui"]:
                        await ui.send_json({"type": "alarm_fire", "id": a.id, "kind": a.kind, "label": a.label})
                        if a.kind == "reminder":
                            self._soon(self._ui_reminder(ui, a))
                        else:
                            ui.ringing = a.id
                    if not self.devices():
                        self.alarms.fired(a.id, now)
                if len(self.ui_fired) > 500:
                    self.ui_fired = {k for k in self.ui_fired if k[1] > now - 3600}
                if len(self.fired_cache) > 100:
                    self.fired_cache = dict(list(self.fired_cache.items())[-50:])
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("scheduler tick failed")

    async def _ui_reminder(self, ui: Session, a: Alarm) -> None:
        await asyncio.sleep(1.6)
        await self.announce(ui, f"Here's your reminder: {a.label}." if a.label else "This is your reminder.")

    async def _housekeeping(self) -> None:
        last_push = time.time()
        last_daily = ""
        while True:
            try:
                await asyncio.sleep(15)
                now = time.time()
                idle = now - self.conv.last_active
                if self.conv.transcript and idle > 60:
                    transcript, self.conv.transcript = self.conv.transcript, []
                    result = await self.memory_mgr.learn_from(transcript)
                    self.record("memory_learning", result=result, status=self.memory_mgr.last_status)
                if self.conv.history and idle > 600:
                    self.conv.history = []
                    self.conv.pending = None
                if now - last_push > 3600:
                    last_push = now
                    await self.push_schedule()
                    await self.tts.eleven.refresh_quota()
                local = datetime.now(ZoneInfo(self.settings["timezone"]))
                day = local.strftime("%Y-%m-%d")
                if local.hour == 4 and last_daily != day:
                    last_daily = day
                    self.alarms.cleanup()
                    if self.memory_mgr.needs_compaction():
                        r = await self.memory_mgr.compact()
                        self.record("memory_compaction", result=r)
                    await self.listener.discover(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")

    def llm_status(self) -> dict:
        """Claude (the brain) plus the Groq listener, in the shape the web page expects."""
        g = self.listener.status()
        claude = self.llm.status()
        groq_err = g["last_error"] if self.settings.secret("GROQ_API_KEY") else ""
        return {
            "brain": "claude", "claude": claude,
            "fast": [claude["fast"]], "smart": [claude["smart"]],
            "listener": g["fast"][:1], "available": g["available"],
            "cooldowns": g["cooldowns"], "limits": g["limits"],
            "last_error": claude["last_error"] or (f"listener: {groq_err}" if groq_err else ""),
        }

    def status(self) -> dict:
        return {
            "sessions": [{"id": s.id, "role": s.role, "name": s.name, "state": s.state, "hands_free": s.hands_free,
                          "since": s.connected_at, "info": s.info, "ringing": s.ringing}
                         for s in self.sessions.values()],
            "keys": self.settings.secret_status(),
            "llm": self.llm_status(),
            "stt": self.stt.status(),
            "listening": {"awake": self.awake, "room": self.room_listening_active()},
            "tts": self.tts.status(),
            "memory": {"count": len(self.memory), "status": self.memory_mgr.last_status},
            "weather_error": self.weather.last_error,
            "spotify": self.spotify.status(),
            "music": self.music.status(),
            "alarm_sounds": self.sounds.status(),
            "voiceid": self.voiceid.status(float(self.settings["voice_trust_seconds"])),
        }
