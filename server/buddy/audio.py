"""Small audio helpers shared by STT, TTS and the session."""
from __future__ import annotations

import io
import wave

import numpy as np

SAMPLE_RATE = 16000


def pcm_to_wav(pcm: np.ndarray | bytes, rate: int = SAMPLE_RATE) -> bytes:
    data = pcm.astype(np.int16).tobytes() if isinstance(pcm, np.ndarray) else pcm
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(data)
    return buf.getvalue()


def resample(x: np.ndarray, src: int, dst: int = SAMPLE_RATE) -> np.ndarray:
    """Band-limited resample via FFT; fine for whole utterances."""
    if src == dst or len(x) == 0:
        return x.astype(np.int16)
    n_out = int(round(len(x) * dst / src))
    spec = np.fft.rfft(x.astype(np.float64))
    keep = n_out // 2 + 1
    if keep <= len(spec):
        spec = spec[:keep]
    else:
        spec = np.concatenate([spec, np.zeros(keep - len(spec))])
    y = np.fft.irfft(spec, n_out) * (n_out / len(x))
    return np.clip(y, -32768, 32767).astype(np.int16)


def rms_dbfs(pcm: np.ndarray) -> float:
    if len(pcm) == 0:
        return -120.0
    r = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
    return 20 * np.log10(max(r, 1e-9) / 32768.0)
