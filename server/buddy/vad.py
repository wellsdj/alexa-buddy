"""Silero VAD (v4, from openWakeWord's release) plus an end-of-speech tracker."""
from __future__ import annotations

import threading

import numpy as np
import onnxruntime as ort

from .config import MODELS_DIR

CHUNK = 512  # 32 ms at 16 kHz, the size Silero v4 is tuned for


class _VadModel:
    _instance: "_VadModel | None" = None
    _lock = threading.Lock()

    def __init__(self):
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 1
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(MODELS_DIR / "silero_vad.onnx"), sess_options=opts, providers=["CPUExecutionProvider"])

    @classmethod
    def shared(cls) -> "_VadModel":
        with cls._lock:
            if cls._instance is None:
                cls._instance = _VadModel()
            return cls._instance


class StreamingVad:
    def __init__(self):
        self.model = _VadModel.shared()
        self.reset()

    def reset(self) -> None:
        self.h = np.zeros((2, 1, 64), dtype=np.float32)
        self.c = np.zeros((2, 1, 64), dtype=np.float32)
        self.pending = np.zeros(0, dtype=np.float32)

    def feed(self, pcm: np.ndarray) -> list[float]:
        """int16 PCM in, one speech probability per 32 ms chunk out."""
        self.pending = np.concatenate([self.pending, pcm.astype(np.float32) / 32768.0])
        probs = []
        sr = np.array(16000, dtype=np.int64)
        while len(self.pending) >= CHUNK:
            chunk, self.pending = self.pending[:CHUNK], self.pending[CHUNK:]
            out, self.h, self.c = self.model.session.run(
                None, {"input": chunk[None, :], "sr": sr, "h": self.h, "c": self.c})
            probs.append(float(out.ravel()[0]))
        return probs


class Endpointer:
    """Decides when a spoken command has finished.

    speech starts after `start_chunks` consecutive voiced chunks; it ends after
    `silence_ms` of unvoiced audio. Gives up if nobody speaks within `no_speech_s`.
    """

    def __init__(self, silence_ms: int = 800, no_speech_s: float = 6.0, max_s: float = 15.0,
                 on: float = 0.5, off: float = 0.35, start_chunks: int = 2):
        self.chunk_s = CHUNK / 16000
        self.silence_chunks = max(1, int(silence_ms / 1000 / self.chunk_s))
        self.no_speech_chunks = int(no_speech_s / self.chunk_s)
        self.max_chunks = int(max_s / self.chunk_s)
        self.on, self.off, self.start_chunks = on, off, start_chunks
        self.chunks = 0
        self.voiced_run = 0
        self.silent_run = 0
        self.started = False
        self.speech_chunks = 0

    def update(self, probs: list[float]) -> str | None:
        """Returns None while listening, else 'end', 'timeout' or 'max'."""
        for p in probs:
            self.chunks += 1
            if p >= self.on:
                self.voiced_run += 1
                self.silent_run = 0
                self.speech_chunks += 1
                if self.voiced_run >= self.start_chunks:
                    self.started = True
            elif p < self.off:
                self.voiced_run = 0
                self.silent_run += 1
            if self.started and self.silent_run >= self.silence_chunks:
                return "end"
            if not self.started and self.chunks >= self.no_speech_chunks:
                return "timeout"
            if self.chunks >= self.max_chunks:
                return "max"
        return None

    @property
    def speech_seconds(self) -> float:
        return self.speech_chunks * self.chunk_s
