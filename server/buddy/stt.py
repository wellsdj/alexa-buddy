"""Speech-to-text with Groq Whisper (free tier: 20 req/min, 2000/day)."""
from __future__ import annotations

import asyncio
import logging
import re
import time

import httpx
import numpy as np

from .audio import pcm_to_wav
from .config import Settings

log = logging.getLogger("buddy.stt")
GROQ_BASE = "https://api.groq.com/openai/v1"

# Whisper invents these on near-silent audio; drop them unless the VAD heard real speech.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thanks for watching", "bye",
    "thank you for watching", "so", "uh", "um", "hmm",
    "subtitles by the amara.org community", ".", "...", "you.",
}


class STTError(Exception):
    pass


class GroqSTT:
    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        self.last_ms = 0

    async def transcribe(self, pcm: np.ndarray, prompt: str = "", speech_seconds: float = 1.0) -> str:
        key = self.settings.secret("GROQ_API_KEY")
        if not key:
            raise STTError("No Groq API key set")
        wav = pcm_to_wav(pcm)
        data = {
            "model": self.settings["stt_model"],
            "language": "en",
            "response_format": "json",
            "temperature": "0",
        }
        if prompt:
            data["prompt"] = prompt[:800]
        t0 = time.monotonic()
        for attempt in range(3):
            try:
                r = await self.client.post(
                    f"{GROQ_BASE}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    data=data,
                    files={"file": ("speech.wav", wav, "audio/wav")},
                )
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise STTError(f"network error: {e}") from e
                continue
            if r.status_code == 429 and attempt < 2:
                wait = min(float(r.headers.get("retry-after", "1") or 1), 4.0)
                log.warning("STT rate limited, retrying in %.1fs", wait)
                await asyncio.sleep(wait)
                continue
            if r.status_code != 200:
                raise STTError(f"Groq STT {r.status_code}: {r.text[:200]}")
            break
        self.last_ms = int((time.monotonic() - t0) * 1000)
        text = (r.json().get("text") or "").strip()
        return clean_transcript(text, speech_seconds)


def clean_transcript(text: str, speech_seconds: float) -> str:
    norm = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if speech_seconds < 0.2:
        return ""
    if norm in HALLUCINATIONS and speech_seconds < 0.45:
        return ""
    return text
