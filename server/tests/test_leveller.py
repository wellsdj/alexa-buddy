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


def test_volume_glides_instead_of_jumping():
    # Loud then quiet speech makes the leveller change gain; it must never step suddenly
    # (a step in gain on a voice sounds like crackle).
    raw = np.frombuffer(_talk(-3, 1.5) + _talk(-30, 1.5), dtype=np.int16).astype(float)
    y = _level(raw.astype(np.int16).tobytes()).astype(float)
    keep = np.abs(raw) > 2000
    gain = y[keep] / raw[keep]
    assert np.max(np.abs(np.diff(gain))) < 0.06


def test_pure_tone_stays_clean():
    t = np.arange(16000 * 2) / 16000
    raw = (32767 * 0.9 * np.sin(2 * np.pi * 1000 * t)).astype(np.int16).tobytes()
    y = _level(raw).astype(float)[8000:-4000]
    spectrum = np.abs(np.fft.rfft(y * np.hanning(len(y))))
    freqs = np.fft.rfftfreq(len(y), 1 / 16000)
    tone = spectrum[np.abs(freqs - 1000) < 20].max()
    rest = np.sqrt(np.sum(spectrum[(freqs > 50) & (np.abs(freqs - 1000) > 30)] ** 2))
    assert rest / tone < 0.005  # under 0.5% distortion
