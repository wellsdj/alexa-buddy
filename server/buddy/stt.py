"""Speech-to-text on this computer: Moonshine (base, English) through sherpa-onnx.

Free, private (audio never leaves the house) and quick: about a tenth of a second per
sentence, which is what lets Buddy listen to the whole room without a cloud service.
setup_models.py downloads the model (about 240 MB) into models/ears/.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import time

import numpy as np

from .config import MODELS_DIR, Settings

log = logging.getLogger("buddy.stt")

# Parakeet (NVIDIA, English): the accurate one, ~480 MB. Moonshine: the small backup.
PARAKEET_DIR = MODELS_DIR / "ears" / "parakeet-tdt-0.6b-v2"
PARAKEET_FILES = ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt")
EARS_DIR = MODELS_DIR / "ears" / "moonshine-base-en"
EARS_FILES = ("preprocess.onnx", "encode.int8.onnx", "uncached_decode.int8.onnx",
              "cached_decode.int8.onnx", "tokens.txt")
NOT_INSTALLED = "Buddy's ears aren't downloaded yet. Double-click Update Buddy, then try again."

# Speech models invent these on near-silent audio; drop them unless the VAD heard real speech.
HALLUCINATIONS = {
    "", "you", "thank you", "thanks", "thanks for watching",
    "thank you for watching", "so", "uh", "um", "hmm", ".", "...", "you.",
}


class STTError(Exception):
    pass


class LocalSTT:
    def __init__(self, settings: Settings, models_dir=None, threads: int | None = None):
        self.settings = settings
        self.fixed_dir = models_dir  # tests point this at a specific Moonshine folder
        self.threads = threads or max(2, min(4, (os.cpu_count() or 2) - 1))
        self._recognizer = None
        self._engine = ""
        self._lock = threading.Lock()
        self.last_ms = 0

    def _which(self) -> str:
        if self.fixed_dir is None and all((PARAKEET_DIR / f).exists() for f in PARAKEET_FILES):
            return "parakeet"
        d = self.fixed_dir or EARS_DIR
        return "moonshine" if all((d / f).exists() for f in EARS_FILES) else ""

    @property
    def models_dir(self):
        return PARAKEET_DIR if self._which() == "parakeet" else (self.fixed_dir or EARS_DIR)

    def available(self) -> bool:
        return bool(self._which())

    def _load(self):
        import sherpa_onnx
        self._engine = self._which()
        d = self.models_dir
        if self._engine == "parakeet":
            return sherpa_onnx.OfflineRecognizer.from_transducer(
                encoder=str(d / "encoder.int8.onnx"), decoder=str(d / "decoder.int8.onnx"),
                joiner=str(d / "joiner.int8.onnx"), tokens=str(d / "tokens.txt"),
                model_type="nemo_transducer", num_threads=self.threads)
        return sherpa_onnx.OfflineRecognizer.from_moonshine(
            preprocessor=str(d / "preprocess.onnx"), encoder=str(d / "encode.int8.onnx"),
            uncached_decoder=str(d / "uncached_decode.int8.onnx"), cached_decoder=str(d / "cached_decode.int8.onnx"),
            tokens=str(d / "tokens.txt"), num_threads=self.threads)

    def warm(self) -> None:
        """Load the model now, so the first sentence isn't slow."""
        if self.available():
            with self._lock:
                if self._recognizer is None:
                    self._recognizer = self._load()
                    log.info("ears loaded from %s", self.models_dir)

    def _decode(self, pcm: np.ndarray) -> str:
        with self._lock:  # one recognizer, one sentence at a time
            if self._recognizer is None:
                self._recognizer = self._load()
                log.info("ears loaded from %s", self.models_dir)
            stream = self._recognizer.create_stream()
            stream.accept_waveform(16000, pcm.astype(np.float32) / 32768.0)
            self._recognizer.decode_stream(stream)
            return stream.result.text.strip()

    async def transcribe(self, pcm: np.ndarray, prompt: str = "", speech_seconds: float = 1.0) -> str:
        if not self.available():
            raise STTError(NOT_INSTALLED)
        if len(pcm) < 16000 * 0.2:
            return ""
        t0 = time.monotonic()
        try:
            text = await asyncio.to_thread(self._decode, pcm)
        except Exception as e:  # noqa: BLE001 - a broken model file shouldn't take Buddy down
            log.exception("speech recognition failed")
            raise STTError(f"speech recognition failed: {e}") from e
        self.last_ms = int((time.monotonic() - t0) * 1000)
        return clean_transcript(text, speech_seconds)

    def status(self) -> dict:
        name = {"parakeet": "Parakeet", "moonshine": "Moonshine"}.get(self._engine or self._which(), "none")
        return {"engine": f"{name} (on this computer)", "installed": self.available(), "last_ms": self.last_ms}


def clean_transcript(text: str, speech_seconds: float) -> str:
    norm = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if speech_seconds < 0.1:  # short words like "off" are only ~0.2 s of speech
        return ""
    if norm in HALLUCINATIONS and speech_seconds < 0.45:
        return ""
    return text
