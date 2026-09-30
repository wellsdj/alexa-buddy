"""Connections and the voice loop.

Every WebSocket client is a Session. The ESP32 is a session with role "device"; each
open web page is a session with role "ui" and can act as a virtual device (streaming its
microphone), so the web page exercises exactly the same code path as the hardware.

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
import time
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np
from fastapi import WebSocket

from . import intents
from .alarms import Alarm, AlarmStore, DAY_NAMES, spoken_time
from .audio import rms_dbfs
from .brain import Brain, Conversation, Reply
from .config import Settings
from .llm import GroqLLM
from .memory import MemoryManager, MemoryStore
from .stt import GroqSTT, STTError
from .tts import ElevenLabsTTS, TTSRouter, TTSUnavailable
from .vad import CHUNK as VAD_CHUNK
from .vad import Endpointer, StreamingVad
from .wakeword import WakeDetector
from .weather import Weather

log = logging.getLogger("buddy.hub")
SR = 16000
BYTES_PER_S = SR * 2


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
        self.verify = False
        self.ptt = False
        self.follow = False
        self.inbox: asyncio.Queue = asyncio.Queue(maxsize=400)
        self.worker: asyncio.Task | None = None
        self.speak_task: asyncio.Task | None = None
        self.seq = 0
        self.playback_done = asyncio.Event()
        self.ringing: str | None = None
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
        await self.send_json({"type": "state", "state": state})
        await self.hub.broadcast_ui({"type": "event", "kind": "state", "session": self.id,
                                     "role": self.role, "state": state}, exclude=self)

    async def earcon(self, name: str) -> None:
        await self.send_json({"type": "earcon", "name": name})

    # ---------- audio in ----------
    def feed(self, pcm_bytes: bytes) -> None:
        if not self.hands_free and not self.ptt and self.state != "listening":
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
                    run_wake = self.hands_free and (self.state != "listening" or self.verify)
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

        if self.state == "listening":
            self.capture.append(pcm)
            if self.verify and wake and wake.confident:
                self.verify = False
                await self.earcon("wake")
                await self.set_state("listening", force=True)  # it was listening silently until now
                await self.hub.broadcast_ui({"type": "event", "kind": "wake", "session": self.id,
                                             "score": round(wake.score, 3), "confident": True})
            if self.ptt:
                return
            result = self.endpointer.update(probs)
            if result:
                await self._finish_capture(result)
            return

        if wake is None or not self.hands_free:
            return
        if wake.confident and (self.state in ("idle", "speaking") or self.ringing):
            await self.hub.broadcast_ui({"type": "event", "kind": "wake", "session": self.id,
                                         "score": round(wake.score, 3), "confident": True})
            if self.state == "speaking":
                await self.stop_speaking()
            if self.ringing:
                await self.send_json({"type": "alarm_stop"})
            await self.start_listening(preroll_s=0.8, verify=False, earcon=True)
        elif self.state == "idle" and self._verify_allowed(now):
            self.verify_times.append(now)
            await self.start_listening(preroll_s=2.2, verify=True, earcon=False)

    def _verify_allowed(self, now: float) -> bool:
        recent = [t for t in self.verify_times if now - t < 3600]
        if recent and now - recent[-1] < 4:
            return False
        return len(recent) < 90  # keep "maybe" checks well inside Groq's free STT quota

    # ---------- listening ----------
    async def start_listening(self, preroll_s: float = 0.0, verify: bool = False, earcon: bool = True,
                              ptt: bool = False, no_speech_s: float = 6.0) -> None:
        s = self.hub.settings
        self.verify = verify
        self.ptt = ptt
        pre = self._take_preroll(preroll_s) if preroll_s else np.zeros(0, dtype=np.int16)
        self.capture = [pre] if len(pre) else []
        self.endpointer = Endpointer(silence_ms=int(s["end_of_speech_ms"]), no_speech_s=no_speech_s,
                                     max_s=float(s["max_listen_s"]) + preroll_s)
        if preroll_s:
            n = int(preroll_s * SR / VAD_CHUNK)
            self.endpointer.update(list(self.vad_history)[-n:])
        if self.detector and not verify:
            self.detector.reset()
        if earcon:
            await self.earcon("wake")
        if not verify:
            await self.set_state("listening")
        else:
            self.state = "listening"  # silent: no LED, no chime until the transcript confirms it

    async def end_ptt(self) -> None:
        if self.state == "listening" and self.ptt:
            await self._finish_capture("end")

    async def _finish_capture(self, reason: str) -> None:
        audio = np.concatenate(self.capture) if self.capture else np.zeros(0, dtype=np.int16)
        verify, self.verify, self.ptt = self.verify, False, False
        speech_s = self.endpointer.speech_seconds if self.endpointer else 0.0
        self.capture = []
        if reason == "timeout" or len(audio) < SR * 0.3:
            self.state = "idle"
            await self.send_json({"type": "state", "state": "idle"})
            if not verify:
                await self.earcon("nospeech")
            return
        if verify:
            self.state = "thinking"  # still silent until the transcript proves it was "hey buddy"
        else:
            await self.set_state("thinking")
            await self.earcon("end")
        asyncio.create_task(self.hub.process_audio(self, audio, speech_s, verify))

    # ---------- speaking ----------
    async def speak(self, text: str, then_listen: bool = False) -> None:
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        self.speak_task = asyncio.create_task(self._speak(text, then_listen))
        try:
            await self.speak_task
        except asyncio.CancelledError:
            pass

    async def _speak(self, text: str, then_listen: bool) -> None:
        if not text:
            await self.set_state("idle")
            return
        self.seq = (self.seq + 1) % 256
        seq = self.seq
        self.playback_done.clear()
        await self.set_state("speaking")
        await self.send_json({"type": "audio_start", "seq": seq, "rate": SR, "text": text})
        sent = 0
        t0 = time.monotonic()
        lead = 0.6
        try:
            async for chunk in self.hub.tts.stream(text):
                for i in range(0, len(chunk), 3200):  # 100 ms frames
                    part = chunk[i:i + 3200]
                    ahead = sent / BYTES_PER_S - (time.monotonic() - t0)
                    if ahead > lead:
                        await asyncio.sleep(ahead - lead)
                    await self.send_audio(seq, part)
                    sent += len(part)
        except TTSUnavailable as e:
            log.warning("no TTS: %s", e)
            await self.send_json({"type": "say", "seq": seq, "text": text})
            if self.role == "device":
                await self.earcon("error")
            sent = int(len(text) / 14 * BYTES_PER_S)  # rough speaking time for the browser voice
        except Exception:  # noqa: BLE001
            log.exception("speaking failed")
        await self.send_json({"type": "audio_end", "seq": seq, "bytes": sent})
        remaining = max(0.0, sent / BYTES_PER_S - (time.monotonic() - t0))
        try:
            await asyncio.wait_for(self.playback_done.wait(), timeout=remaining + 4.0)
        except asyncio.TimeoutError:
            pass
        if then_listen and self.hands_free:
            await self.start_listening(earcon=True, no_speech_s=5.0)
        else:
            await self.set_state("idle")

    async def stop_speaking(self) -> None:
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
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
    def __init__(self, settings: Settings, http=None):
        self.settings = settings
        d = settings.data_dir
        self.llm = GroqLLM(settings, http)
        self.stt = GroqSTT(settings, http)
        self.tts = TTSRouter(settings, ElevenLabsTTS(settings, http))
        self.weather = Weather(settings, http)
        self.alarms = AlarmStore(d / "alarms.json", settings["timezone"])
        self.memory = MemoryStore(d / "memory.json")
        self.memory_mgr = MemoryManager(self.memory, self.llm, settings)
        self.brain = Brain(settings, self.llm, self.alarms, self.memory, self.memory_mgr, self.weather)
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
        if "volume" in changes:
            self.brain.volume = int(changes["volume"])
            self._soon(self.broadcast({"type": "volume", "level": int(changes["volume"])}, role="device"))
        if "device_mic_shift" in changes:
            self._soon(self.broadcast({"type": "mic_gain", "shift": int(changes["device_mic_shift"])}, role="device"))
        self._soon(self.broadcast_ui({"type": "event", "kind": "settings"}))

    # ---------- lifecycle ----------
    async def start(self) -> None:
        self.tasks.append(asyncio.create_task(self._scheduler()))
        self.tasks.append(asyncio.create_task(self._housekeeping()))
        asyncio.create_task(self.llm.discover())
        asyncio.create_task(self.tts.eleven.refresh_quota())
        asyncio.create_task(self.weather.fetch())

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()

    def add(self, s: Session) -> None:
        self.sessions[s.id] = s

    def remove(self, s: Session) -> None:
        self.sessions.pop(s.id, None)
        for t in (s.worker, s.speak_task):
            if t and not t.done():
                t.cancel()

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
                           "mic_shift": int(self.settings["device_mic_shift"])})
        if s.role == "device":
            await self.push_schedule(s)
        self.record("connect", session=s.id, role=s.role, info=s.info)

    async def push_schedule(self, only: Session | None = None) -> None:
        msg = {"type": "alarms", "server_time": int(time.time()), "fires": self.alarms.device_schedule()}
        for s in ([only] if only else self.devices()):
            await s.send_json(msg)
        if not only:
            await self.broadcast_ui({"type": "event", "kind": "alarms"})

    # ---------- the pipeline ----------
    def _stt_prompt(self) -> str:
        name = self.settings["user_name"]
        return f"Hey Buddy. {('I am ' + name + '. ') if name else ''}Set an alarm, what's the weather in Richmond?"

    async def process_audio(self, s: Session, audio: np.ndarray, speech_s: float, verify: bool) -> None:
        t0 = time.monotonic()
        try:
            text = await self.stt.transcribe(audio, self._stt_prompt(), speech_s)
        except STTError as e:
            log.warning("STT failed: %s", e)
            self.record("error", where="stt", error=str(e))
            if verify:
                await s.set_state("idle")
                return
            msg = ("I need a Groq API key to understand speech. Add it on the web page."
                   if "No Groq API key" in str(e) else "Sorry, I couldn't hear that properly. Try again?")
            await self.announce(s, msg)
            return
        stt_ms = int((time.monotonic() - t0) * 1000)
        if verify:
            words = text.lower().split()
            if not intents.has_wake(text) and not any("budd" in w or w.strip(",.!?") == "buddy" for w in words[:4]):
                self.record("wake_rejected", session=s.id, heard=text)
                s.state = "idle"
                await s.send_json({"type": "state", "state": "idle"})
                return
            self.record("wake_verified", session=s.id, heard=text)
            await s.earcon("wake")
            await s.set_state("thinking")
            if not intents.strip_wake(text):
                await s.start_listening(earcon=False, no_speech_s=6.0)
                return
        if not text.strip():
            await self.announce(s, "Sorry, I didn't catch that.")
            return
        await self.respond(s, text, stt_ms=stt_ms)

    async def process_text(self, s: Session, text: str) -> None:
        await s.set_state("thinking")
        await self.respond(s, text)

    async def announce(self, s: Session, text: str) -> None:
        """Speak something that isn't a reply to what was just said, and show it in the chat."""
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant", "text": text})
        await s.speak(text)

    async def respond(self, s: Session, text: str, stt_ms: int = 0) -> None:
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "user", "text": text})
        ctx = {"ringing": s.ringing or next((d.ringing for d in self.devices() if d.ringing), None)}
        try:
            reply: Reply = await self.brain.handle(text, self.conv, ctx)
        except Exception as e:  # noqa: BLE001
            log.exception("brain failed")
            reply = Reply("Sorry, something went wrong on my side.", intent="error")
            self.record("error", where="brain", error=str(e))
        for act in reply.actions:
            await self.apply_action(s, act)
        self.record("turn", session=s.id, heard=text, said=reply.text, intent=reply.intent, model=reply.model,
                    stt_ms=stt_ms, brain_ms=reply.ms)
        await self.broadcast_ui({"type": "event", "kind": "transcript", "session": s.id, "role": "assistant",
                                 "text": reply.text, "model": reply.model, "intent": reply.intent})
        if reply.text:
            await s.speak(reply.text, then_listen=reply.follow_up)
        else:
            await s.set_state("idle")

    async def apply_action(self, s: Session, act: dict) -> None:
        kind = act.get("type")
        if kind == "volume":
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
            await asyncio.sleep(1.6)  # let the chime finish first
            text = f"Here's your reminder: {a.label}." if a and a.label else "This is your reminder."
            await self.announce(s, text)
        else:
            s.ringing = alarm_id
            await self.broadcast_ui({"type": "event", "kind": "ringing", "session": s.id, "id": alarm_id,
                                     "label": a.label if a else "", "alarm_kind": kind})

    async def alarm_stopped(self, s: Session, alarm_id: str | None, reason: str) -> None:
        a = self._resolve(alarm_id)
        s.ringing = None
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
                    await self.llm.discover(force=True)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("housekeeping failed")

    def status(self) -> dict:
        return {
            "sessions": [{"id": s.id, "role": s.role, "name": s.name, "state": s.state, "hands_free": s.hands_free,
                          "since": s.connected_at, "info": s.info, "ringing": s.ringing}
                         for s in self.sessions.values()],
            "keys": self.settings.secret_status(),
            "llm": self.llm.status(),
            "tts": self.tts.status(),
            "memory": {"count": len(self.memory), "status": self.memory_mgr.last_status},
            "weather_error": self.weather.last_error,
        }
