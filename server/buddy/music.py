"""Music through Buddy's own speaker (the radio), via Spotify Connect.

librespot (a free, open-source Spotify Connect speaker, installed with Homebrew) runs on this
computer and shows up in Spotify as a speaker called "Buddy". Whatever plays on it comes out of
librespot as raw audio; this module turns it into the desk unit's format and streams it to the
board, which plays it on the radio. Buddy pauses the music while it listens and talks.

librespot isn't made by Spotify (it's technically outside Spotify's terms) and needs Premium.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time

import numpy as np

from .config import Settings

log = logging.getLogger("buddy.music")

IN_RATE = 44100  # librespot's pipe output: 44.1 kHz, stereo, 16-bit
IN_BYTES_PER_S = IN_RATE * 4
SEARCH_PATHS = ("/opt/homebrew/bin/librespot", "/usr/local/bin/librespot")


def find_librespot() -> str | None:
    return shutil.which("librespot") or next((p for p in SEARCH_PATHS if os.path.exists(p)), None)


class Resampler:
    """44.1 kHz stereo -> mono at the board's rate. Uses soxr (high quality, streaming) when
    installed, otherwise a simple linear resampler."""

    def __init__(self, out_rate: int):
        self.out_rate = out_rate
        self.stream = None
        try:
            import soxr
            self.stream = soxr.ResampleStream(IN_RATE, out_rate, 1, dtype="float32", quality="HQ")
        except Exception:  # noqa: BLE001 - soxr missing: fall back
            self.pos = 0.0
            self.last = 0.0

    def process(self, pcm: bytes, last: bool = False) -> bytes:
        x = np.frombuffer(pcm[: len(pcm) - len(pcm) % 4], dtype="<i2").astype(np.float32)
        mono = (x[0::2] + x[1::2]) * 0.5 / 32768.0
        if self.stream is not None:
            y = self.stream.resample_chunk(mono, last=last)
        else:
            step = IN_RATE / self.out_rate
            src = np.concatenate([[self.last], mono])
            idx = np.arange(self.pos, len(src) - 1, step)
            y = np.interp(idx, np.arange(len(src)), src) if len(idx) else np.zeros(0, np.float32)
            self.pos = (idx[-1] + step - (len(src) - 1)) if len(idx) else self.pos - (len(src) - 1)
            self.last = src[-1]
        return (np.clip(y, -1.0, 1.0) * 32000).astype("<i2").tobytes()


class MusicPlayer:
    """Runs librespot and streams what it plays to the desk unit(s)."""

    def __init__(self, settings: Settings, hub):
        self.settings = settings
        self.hub = hub
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.playing = False  # sound is coming out right now
        self.error = ""
        self.binary = find_librespot()
        # Where the sound got to, for the Activity tab: in from librespot, out to the board.
        self.bytes_in = 0
        self.bytes_out = 0
        self.busy_drops = 0  # bits dropped because Buddy was listening or talking
        self.no_unit_drops = 0  # bits dropped because no desk unit was connected
        self.rate = 0
        from collections import deque
        self.log_tail: deque = deque(maxlen=6)

    @property
    def name(self) -> str:
        return (self.settings["assistant_name"] or "Buddy").strip()

    def status(self) -> dict:
        return {"enabled": bool(self.settings["music_on_buddy"]), "installed": bool(self.binary),
                "running": self.proc is not None and self.proc.returncode is None,
                "playing": self.playing, "name": self.name, "error": self.error,
                "kb_in": self.bytes_in // 1024, "kb_out": self.bytes_out // 1024, "rate": self.rate,
                "busy_drops": self.busy_drops, "no_unit_drops": self.no_unit_drops,
                "librespot_log": list(self.log_tail)}

    async def start(self) -> None:
        if not self.settings["music_on_buddy"]:
            return
        self.binary = find_librespot()
        if not self.binary:
            self.error = "librespot isn't installed (Update Buddy installs it if Homebrew is on this Mac)."
            log.info(self.error)
            return
        cache = self.settings.data_dir / "librespot"
        cache.mkdir(parents=True, exist_ok=True)
        args = [self.binary, "--name", self.name, "--backend", "pipe", "--format", "S16",
                "--bitrate", "320", "--device-type", "speaker", "--initial-volume", "80",
                "--enable-volume-normalisation", "--cache", str(cache), "--disable-audio-cache"]
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError as e:
            self.error = f"couldn't start librespot: {e}"
            log.warning(self.error)
            return
        self.error = ""
        log.info("music: %s is available as a Spotify speaker", self.name)
        self.task = asyncio.create_task(self._pump())
        asyncio.create_task(self._log_stderr())

    async def stop(self) -> None:
        if self.task:
            self.task.cancel()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), 5)
            except asyncio.TimeoutError:
                self.proc.kill()
        self.proc = None

    async def restart(self) -> None:
        await self.stop()
        await self.start()

    async def _log_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        async for line in self.proc.stderr:
            text = line.decode(errors="ignore").strip()
            if text:
                self.log_tail.append(text[-200:])
                log.info("librespot: %s", text[:300])

    async def _pump(self) -> None:
        """Read librespot's audio at real time and send it on. A gap of half a second (paused,
        between songs) ends the stream; the next sound starts a new one."""
        assert self.proc and self.proc.stdout
        out = self.proc.stdout
        stream = None  # (sessions, seq per session, resampler, started_at, bytes_sent, rate)
        chunk = IN_BYTES_PER_S // 20  # 50 ms
        while True:
            try:
                data = await asyncio.wait_for(out.read(chunk), timeout=0.5)
            except asyncio.TimeoutError:
                data = None
            if data == b"":  # librespot quit
                self.playing = False
                if stream:
                    await self._end(stream)
                self.error = "librespot stopped"
                log.warning("music: librespot stopped")
                return
            if data is None:
                if stream:
                    await self._end(stream)
                    stream = None
                self.playing = False
                continue
            self.bytes_in += len(data)
            devices = self.hub.devices()
            targets = [s for s in devices if s.state == "idle"]
            if not targets:
                if devices:
                    self.busy_drops += 1
                else:
                    self.no_unit_drops += 1
                # Buddy is listening or talking (the music should be paused), or no desk unit:
                # drop this bit rather than talk over each other.
                if stream:
                    await self._end(stream)
                    stream = None
                continue
            if stream is None or {s.id for s in stream[0]} != {s.id for s in targets}:
                if stream:
                    await self._end(stream)
                stream = await self._begin(targets)
            sessions, seqs, rs, started, sent, rate = stream
            pcm = rs.process(data)
            if not pcm:
                continue
            bytes_per_s = rate * 2
            ahead = sent / bytes_per_s - (time.monotonic() - started)
            if ahead > 0.6:
                await asyncio.sleep(ahead - 0.6)  # keep about 0.6 s ahead of the board
            for s in sessions:
                for i in range(0, len(pcm), 3200):
                    await s.send_audio(seqs[s.id], pcm[i:i + 3200])
            self.bytes_out += len(pcm)
            stream = (sessions, seqs, rs, started, sent + len(pcm), rate)
            self.playing = True

    async def _begin(self, sessions):
        # Newer board software plays music at 32 kHz; older plays everything at 16 kHz.
        rate = 32000 if all(_fw(s) >= (1, 2) for s in sessions) else 16000
        seqs = {}
        for s in sessions:
            s.seq = (s.seq + 1) % 256
            seqs[s.id] = s.seq
            await s.send_json({"type": "audio_start", "seq": s.seq, "rate": rate, "text": "", "music": True})
        self.rate = rate
        log.info("music: streaming to %d desk unit(s) at %d Hz", len(sessions), rate)
        return (sessions, seqs, Resampler(rate), time.monotonic(), 0, rate)

    async def _end(self, stream) -> None:
        sessions, seqs, _rs, _st, sent, _rate = stream
        for s in sessions:
            await s.send_json({"type": "audio_end", "seq": seqs[s.id], "bytes": sent})


def _fw(session) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in str(session.info.get("fw", "0")).split(".")[:2])
    except ValueError:
        return (0,)
