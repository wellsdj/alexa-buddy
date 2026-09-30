"""Text-to-speech. ElevenLabs first, a free offline Piper voice when ElevenLabs is missing,
out of credits or down, and the browser's own voice as a last resort for the web UI.

Everything is produced as 16 kHz mono int16 PCM so the ESP32 can play it directly.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
import numpy as np

from .audio import resample
from .config import Settings

log = logging.getLogger("buddy.tts")
ELEVEN_BASE = "https://api.elevenlabs.io/v1"


class TTSUnavailable(Exception):
    pass


class ElevenLabsTTS:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=5.0))
        self.blocked_until = 0.0
        self.last_error = ""
        self.quota: dict | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.secret("ELEVENLABS_API_KEY")) and time.time() >= self.blocked_until

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        key = self.settings.secret("ELEVENLABS_API_KEY")
        if not key:
            raise TTSUnavailable("no ElevenLabs key")
        voice = self.settings["elevenlabs_voice_id"]
        body = {
            "text": text,
            "model_id": self.settings["elevenlabs_model"],
            "voice_settings": {
                "stability": 0.45,
                "similarity_boost": 0.8,
                "style": 0.0,
                "use_speaker_boost": True,
                "speed": float(self.settings["voice_speed"]),
            },
        }
        url = f"{ELEVEN_BASE}/text-to-speech/{voice}/stream"
        async with self.client.stream(
            "POST", url, params={"output_format": "pcm_16000"},
            headers={"xi-api-key": key, "Accept": "audio/pcm"}, json=body,
        ) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode(errors="ignore")[:300]
                self.last_error = f"{r.status_code}: {detail}"
                if r.status_code in (401, 402) or "quota" in detail.lower():
                    # Out of credits or bad key: stop trying for an hour, Piper takes over.
                    self.blocked_until = time.time() + 3600
                elif r.status_code == 429:
                    self.blocked_until = time.time() + 20
                raise TTSUnavailable(f"ElevenLabs {self.last_error}")
            carry = b""
            async for chunk in r.aiter_bytes():
                data = carry + chunk
                cut = len(data) - (len(data) % 2)
                carry = data[cut:]
                if cut:
                    yield data[:cut]

    async def refresh_quota(self) -> dict | None:
        key = self.settings.secret("ELEVENLABS_API_KEY")
        if not key:
            self.quota = None
            return None
        try:
            r = await self.client.get(f"{ELEVEN_BASE}/user/subscription", headers={"xi-api-key": key})
            if r.status_code == 200:
                j = r.json()
                self.quota = {
                    "used": j.get("character_count"),
                    "limit": j.get("character_limit"),
                    "resets": j.get("next_character_count_reset_unix"),
                    "tier": j.get("tier"),
                }
                if self.quota["used"] is not None and self.quota["limit"]:
                    if self.quota["used"] >= self.quota["limit"]:
                        self.blocked_until = time.time() + 3600
        except httpx.HTTPError as e:
            log.info("quota check failed: %s", e)
        return self.quota


class PiperTTS:
    """Offline fallback. Needs `pip install piper-tts` and a voice in data/voices/."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._voice = None
        self._voice_path: Path | None = None
        self.error = ""

    def _find_voice(self) -> Path | None:
        configured = self.settings["piper_voice"]
        if configured and Path(configured).exists():
            return Path(configured)
        voices = sorted((self.settings.data_dir / "voices").glob("*.onnx"))
        return voices[0] if voices else None

    @property
    def available(self) -> bool:
        try:
            import piper  # noqa: F401
        except ImportError:
            self.error = "piper-tts not installed"
            return False
        return self._find_voice() is not None

    def _load(self):
        path = self._find_voice()
        if path is None:
            raise TTSUnavailable("no Piper voice downloaded (run setup_models.py)")
        if self._voice is None or path != self._voice_path:
            from piper import PiperVoice
            self._voice = PiperVoice.load(str(path))
            self._voice_path = path
        return self._voice

    def synth_blocking(self, text: str) -> bytes:
        from piper.config import SynthesisConfig
        voice = self._load()
        speed = float(self.settings["voice_speed"]) or 1.0
        cfg = SynthesisConfig(length_scale=1.0 / speed)
        parts = [chunk.audio_int16_array for chunk in voice.synthesize(text, syn_config=cfg)]
        if not parts:
            return b""
        pcm = np.concatenate(parts).astype(np.float64) * 0.7  # Piper peaks at full scale; ~-3 dB like ElevenLabs
        return resample(pcm, voice.config.sample_rate, 16000).tobytes()

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        pcm = await asyncio.to_thread(self.synth_blocking, text)
        for i in range(0, len(pcm), 6400):
            yield pcm[i:i + 6400]


class TTSRouter:
    """Picks an engine per utterance and caches short phrases to save ElevenLabs credits."""

    CACHE_MAX_CHARS = 60

    def __init__(self, settings: Settings, eleven: ElevenLabsTTS | None = None, piper: PiperTTS | None = None):
        self.settings = settings
        self.eleven = eleven or ElevenLabsTTS(settings)
        self.piper = piper or PiperTTS(settings)
        self.cache_dir = settings.data_dir / "tts_cache"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.last_engine = ""
        self.first_byte_ms = 0

    def _engines(self) -> list:
        choice = self.settings["tts_engine"]
        if choice == "browser":
            return []
        if choice == "piper":
            return [self.piper]
        if choice == "elevenlabs":
            return [self.eleven, self.piper]
        return [self.eleven, self.piper]

    def _cache_path(self, text: str, engine: str) -> Path:
        key = f"{engine}|{self.settings['elevenlabs_voice_id']}|{self.settings['voice_speed']}|{text}"
        return self.cache_dir / (hashlib.sha1(key.encode()).hexdigest() + ".pcm")

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yields PCM chunks. Raises TTSUnavailable if no engine can speak."""
        text = text.strip()
        if not text:
            return
        errors = []
        t0 = time.monotonic()
        for engine in self._engines():
            name = "elevenlabs" if engine is self.eleven else "piper"
            if not engine.available:
                continue
            cacheable = len(text) <= self.CACHE_MAX_CHARS
            path = self._cache_path(text, name)
            if cacheable and path.exists():
                self.last_engine = name + " (cached)"
                self.first_byte_ms = 0
                data = path.read_bytes()
                for i in range(0, len(data), 6400):
                    yield data[i:i + 6400]
                return
            got_any = False
            collected = bytearray()
            try:
                async for chunk in engine.stream(text):
                    if not got_any:
                        self.first_byte_ms = int((time.monotonic() - t0) * 1000)
                        self.last_engine = name
                    got_any = True
                    if cacheable:
                        collected += chunk
                    yield chunk
            except TTSUnavailable as e:
                errors.append(str(e))
                if got_any:
                    return
                continue
            except (httpx.HTTPError, OSError, RuntimeError) as e:
                errors.append(f"{name}: {e}")
                if got_any:
                    return
                continue
            if got_any:
                if cacheable and collected:
                    path.write_bytes(bytes(collected))
                return
        raise TTSUnavailable("; ".join(errors) or "no voice engine available")

    def status(self) -> dict:
        return {
            "engine_setting": self.settings["tts_engine"],
            "elevenlabs": {
                "key": bool(self.settings.secret("ELEVENLABS_API_KEY")),
                "available": self.eleven.available,
                "last_error": self.eleven.last_error,
                "quota": self.eleven.quota,
            },
            "piper": {"available": self.piper.available, "error": self.piper.error},
            "last_engine": self.last_engine,
        }
