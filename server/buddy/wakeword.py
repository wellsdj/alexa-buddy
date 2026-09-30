"""Streaming "hey buddy" detector.

Mirrors how the hey-buddy model was trained (heybuddy/embeddings.py): every 1920 new
samples (120 ms) take the last 17280 samples (1.08 s) as int16-range floats, compute a
32-bin mel spectrogram (x/10 + 2), cut 4 embedding windows of 76 frames at stride 8,
keep the most recent 16 embeddings and score them with the classifier.
Model files: mel + embedding from openWakeWord v0.5.1 (sha256 matches hey-buddy's own
copies), classifier from github.com/painebenjamin/hey-buddy. Apache-2.0.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import onnxruntime as ort

from .config import MODELS_DIR

SAMPLE_RATE = 16000
WINDOW = 17280
STEP = 1920
EMB_WINDOW = 76
EMB_STRIDE = 8
EMB_FRAMES = 16


def _session(path: Path) -> ort.InferenceSession:
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1
    opts.inter_op_num_threads = 1
    return ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])


class WakeModels:
    _instance: "WakeModels | None" = None
    _lock = threading.Lock()

    def __init__(self, models_dir: Path = MODELS_DIR, wake_model: str = "hey-buddy.onnx"):
        self.mel = _session(models_dir / "melspectrogram.onnx")
        self.emb = _session(models_dir / "embedding_model.onnx")
        self.wake = _session(models_dir / wake_model)

    @classmethod
    def shared(cls) -> "WakeModels":
        with cls._lock:
            if cls._instance is None:
                cls._instance = WakeModels()
            return cls._instance

    def embeddings(self, audio: np.ndarray) -> np.ndarray:
        mel = self.mel.run(None, {"input": audio[None, :].astype(np.float32)})[0]
        mel = np.squeeze(mel) / 10.0 + 2.0
        starts = range(0, mel.shape[0] - EMB_WINDOW + 1, EMB_STRIDE)
        windows = np.stack([mel[j:j + EMB_WINDOW] for j in starts])[:, :, :, None]
        out = self.emb.run(None, {"input_1": windows.astype(np.float32)})[0]
        return out.reshape(-1, 96)

    def score(self, embeddings: np.ndarray) -> float:
        out = self.wake.run(None, {"input": embeddings[None].astype(np.float32)})[0]
        return float(np.asarray(out).ravel()[0])


@dataclass
class WakeEvent:
    score: float
    confident: bool


class WakeDetector:
    """One per audio stream. Feed int16 PCM; returns the scores computed on this call."""

    def __init__(self, models: WakeModels | None = None, refractory_s: float = 2.0):
        self.models = models or WakeModels.shared()
        self.buffer = np.zeros(WINDOW, dtype=np.float32)
        self.pending = 0
        self.embs = np.zeros((0, 96), dtype=np.float32)
        self.refractory_steps = int(refractory_s * SAMPLE_RATE / STEP)
        self.cooldown = 0
        self.last_score = 0.0
        self.peak_score = 0.0

    def reset(self) -> None:
        self.embs = np.zeros((0, 96), dtype=np.float32)
        self.cooldown = self.refractory_steps

    def feed(self, pcm: np.ndarray, threshold: float, verify_threshold: float) -> WakeEvent | None:
        x = pcm.astype(np.float32)
        event = None
        pos = 0
        while pos < len(x):
            take = min(STEP - self.pending, len(x) - pos)
            chunk = x[pos:pos + take]
            self.buffer = np.concatenate([self.buffer[take:], chunk])
            self.pending += take
            pos += take
            if self.pending < STEP:
                continue
            self.pending = 0
            self.embs = np.concatenate([self.embs, self.models.embeddings(self.buffer)])[-EMB_FRAMES:]
            if len(self.embs) < EMB_FRAMES:
                continue
            score = self.models.score(self.embs)
            self.last_score = score
            self.peak_score = max(self.peak_score, score)
            if self.cooldown > 0:
                self.cooldown -= 1
                continue
            if score >= threshold:
                event = WakeEvent(score, True)
                self.cooldown = self.refractory_steps
            elif score >= verify_threshold and (event is None or not event.confident):
                # Scores rise over consecutive steps, so a "maybe" must not start the
                # cooldown or it would swallow the confident hit 120 ms later.
                if event is None or score > event.score:
                    event = WakeEvent(score, False)
        return event

    def take_peak(self) -> float:
        p, self.peak_score = self.peak_score, 0.0
        return p
