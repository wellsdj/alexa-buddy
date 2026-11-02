"""Your own alarm sounds, from a folder on this computer called "sounds for buddy alarm".

Put audio files (mp3, m4a, wav, aiff...) in that folder, on the Desktop, in Documents,
Downloads or your home folder. They're numbered in name order: sound 1, sound 2, ...
They're converted once (with the Mac's built-in afconvert, or ffmpeg) into the desk unit's
format and kept in data/alarm_sounds_cache.
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

from .audio import resample
from .config import Settings

log = logging.getLogger("buddy.alarm_sounds")

FOLDER_NAME = "sounds for buddy alarm"
AUDIO = {".mp3", ".m4a", ".wav", ".aif", ".aiff", ".aac", ".caf", ".flac", ".ogg"}
MAX_SECONDS = 60  # longer files are cut (an alarm loops anyway)


class AlarmSounds:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.cache = settings.data_dir / "alarm_sounds_cache"
        self.error = ""

    def folder(self) -> Path | None:
        custom = (self.settings["alarm_sounds_folder"] or "").strip()
        if custom and Path(custom).expanduser().is_dir():
            return Path(custom).expanduser()
        home = Path.home()
        for p in (home / "Desktop", home / "Documents", home / "Downloads", home, self.settings.data_dir):
            try:
                for child in p.iterdir():
                    if child.is_dir() and child.name.strip().lower() == FOLDER_NAME:
                        return child
            except OSError:
                continue
        return None

    def files(self) -> list[Path]:
        d = self.folder()
        if not d:
            return []
        try:
            return sorted((f for f in d.iterdir() if f.suffix.lower() in AUDIO and not f.name.startswith(".")),
                          key=lambda f: f.name.lower())
        except OSError:
            return []

    def names(self) -> list[str]:
        return [f.stem for f in self.files()]

    def status(self) -> dict:
        d = self.folder()
        return {"folder": str(d) if d else "", "sounds": self.names(), "error": self.error}

    def pcm(self, number: int) -> bytes:
        """Sound `number` (1-based) as 16 kHz mono 16-bit, levelled. b"" if it can't be had."""
        files = self.files()
        if not 1 <= number <= len(files):
            return b""
        src = files[number - 1]
        try:
            stamp = f"{src}|{src.stat().st_mtime}|{src.stat().st_size}"
        except OSError:
            return b""
        self.cache.mkdir(parents=True, exist_ok=True)
        cached = self.cache / (hashlib.sha1(stamp.encode()).hexdigest() + ".pcm")
        if cached.exists():
            return cached.read_bytes()
        try:
            pcm = self._convert(src)
        except Exception as e:  # noqa: BLE001 - a bad file mustn't stop the alarm (the board beeps instead)
            self.error = f"{src.name}: {e}"
            log.warning("couldn't use alarm sound %s: %s", src, e)
            return b""
        cached.write_bytes(pcm)
        return pcm

    def _convert(self, src: Path) -> bytes:
        tmp = self.cache / "convert.wav"
        if src.suffix.lower() == ".wav":
            wav = src
        elif shutil.which("afconvert"):  # every Mac has this
            subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(src), str(tmp)],
                           check=True, capture_output=True, timeout=60)
            wav = tmp
        elif shutil.which("ffmpeg"):
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(tmp)],
                           check=True, capture_output=True, timeout=60)
            wav = tmp
        else:
            raise RuntimeError("no converter (afconvert or ffmpeg) for this file type")
        with wave.open(str(wav)) as w:
            rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
            raw = w.readframes(min(w.getnframes(), rate * MAX_SECONDS))
        if width != 2:
            raise RuntimeError("only 16-bit audio is supported")
        x = np.frombuffer(raw, dtype="<i2").astype(np.float64)
        if ch > 1:
            x = x.reshape(-1, ch).mean(axis=1)
        x = resample(x, rate, 16000).astype(np.float64) if rate != 16000 else x
        peak = np.abs(x).max() or 1.0
        x = x * (32768 * 10 ** (-3 / 20) / peak)  # loud and clear, never clipping
        return np.clip(x, -32768, 32767).astype("<i2").tobytes()
