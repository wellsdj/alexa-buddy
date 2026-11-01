import numpy as np

from buddy.audio import Leveller, rms_dbfs


def _talk(db: float, seconds: float = 3.0) -> bytes:
    t = np.arange(int(16000 * seconds)) / 16000
    a = 32768 * 10 ** (db / 20) * np.sqrt(2)
    x = a * np.sin(2 * np.pi * 220 * t) * (np.sin(2 * np.pi * 2 * t) > 0)  # speech with pauses
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def _level(raw: bytes, step: int = 777) -> np.ndarray:
    lv = Leveller()
    out = b"".join(lv.process(raw[i:i + step]) for i in range(0, len(raw), step)) + lv.flush()
    assert len(out) == len(raw)  # nothing lost or added, odd-sized pieces included
    return np.frombuffer(out, dtype=np.int16)


def test_quiet_and_loud_voices_come_out_the_same():
    levels = [rms_dbfs((y := _level(_talk(db)))[np.abs(y) > 50]) for db in (-30, -20, -10, -3)]
    assert max(levels) - min(levels) < 2.0


def test_never_clips():
    y = _level(_talk(0))
    assert np.abs(y).max() <= 32768 * 10 ** (-1 / 20)
