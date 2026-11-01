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


class Leveller:
    """Keeps every spoken reply at the same loudness, whichever voice made it.

    ElevenLabs, the Piper backup voice and cached phrases all come out at different levels
    (Piper can be ~10 dB louder), so without this the volume seems to jump between answers.
    Works on the stream as it arrives: quick to turn loud speech down, slow to turn quiet speech
    up, and never lets a sample go past -1 dBFS.
    """

    TARGET_RMS = 32768 * 10 ** (-23 / 20)  # about -23 dBFS while talking
    MAX_BOOST = 2.0
    MIN_GAIN = 0.1
    CEILING = 32768 * 10 ** (-1 / 20)
    FRAME = 320  # 20 ms

    def __init__(self):
        self.gain = None  # decided from the first bit of real speech
        self.carry = b""
        self.pending = np.zeros(0, dtype=np.float64)

    def _frame_gains(self, x: np.ndarray) -> np.ndarray:
        gains = np.empty(len(x) // self.FRAME)
        for i in range(len(gains)):
            f = x[i * self.FRAME:(i + 1) * self.FRAME]
            rms = float(np.sqrt(np.mean(f * f)))
            if rms > 32768 * 10 ** (-45 / 20):  # speech, not a pause
                want = min(self.MAX_BOOST, max(self.MIN_GAIN, self.TARGET_RMS / rms))
                if self.gain is None:
                    self.gain = want
                elif want < self.gain:
                    self.gain += (want - self.gain) * 0.3  # loud: turn down fast
                else:
                    self.gain += (want - self.gain) * 0.02  # quiet: turn up slowly
            gains[i] = self.gain if self.gain is not None else 1.0
        return gains

    def process(self, chunk: bytes) -> bytes:
        data = self.carry + chunk
        cut = len(data) - len(data) % 2
        self.carry = data[cut:]
        x = np.concatenate([self.pending, np.frombuffer(data[:cut], dtype=np.int16).astype(np.float64)])
        n = len(x) - len(x) % self.FRAME
        self.pending = x[n:]
        return self._apply(x[:n])

    def flush(self) -> bytes:
        x, self.pending = self.pending, np.zeros(0, dtype=np.float64)
        if not len(x):
            return b""
        padded = np.concatenate([x, np.zeros(self.FRAME - len(x))])
        return self._apply(padded)[:len(x) * 2]

    def _apply(self, x: np.ndarray) -> bytes:
        if not len(x):
            return b""
        g = np.repeat(self._frame_gains(x), self.FRAME)
        y = np.clip(x * g, -self.CEILING, self.CEILING)
        return y.astype(np.int16).tobytes()
