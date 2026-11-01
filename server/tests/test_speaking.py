"""Speech sent to the desk unit: steady, level, and never more than its 1.5 s buffer can hold."""
import asyncio
import json
import time
from types import SimpleNamespace

import numpy as np

from buddy.hub import BYTES_PER_S, Session

BOARD_BUFFER = 48 * 1024  # SPK_BUFFER_BYTES in the firmware


class FakeBoard:
    """Plays at real time from a buffer, like the ESP32, and records what happened."""

    def __init__(self):
        self.session = None
        self.arrivals = []  # (time, bytes)

    async def send_text(self, text):
        msg = json.loads(text)
        if msg["type"] == "audio_end":
            self.session.playback_done.set()

    async def send_bytes(self, data):
        self.arrivals.append((time.monotonic(), len(data) - 1))

    def replay(self):
        """Buffer level over time, playing from the first byte at 32 kB/s."""
        start, level, last, peak, gaps = self.arrivals[0][0], 0.0, self.arrivals[0][0], 0.0, 0
        for t, n in self.arrivals:
            level -= (t - last) * BYTES_PER_S
            if level < -BYTES_PER_S * 0.05:
                gaps += 1  # ran dry for more than 50 ms: an audible gap
            level = max(level, 0.0) + n
            peak, last = max(peak, level), t
        return peak, gaps


class SlowThenFastTTS:
    """A voice service that takes a while to start, then sends everything at once."""
    last_engine = "fake"

    def __init__(self, seconds, first_byte_s, level=0.5):
        t = np.arange(int(16000 * seconds)) / 16000
        self.pcm = (32767 * level * np.sin(2 * np.pi * 220 * t)).astype(np.int16).tobytes()
        self.first_byte_s = first_byte_s

    async def stream(self, text):
        await asyncio.sleep(self.first_byte_s)
        for i in range(0, len(self.pcm), 4096):
            yield self.pcm[i:i + 4096]


def speak(tts):
    board = FakeBoard()

    async def noop(*a, **k):
        return None

    hub = SimpleNamespace(tts=tts, brain=SimpleNamespace(volume=3), record=lambda *a, **k: None,
                          broadcast_ui=noop, last_spoke_at=0)
    s = Session(hub, board, "device", "test")
    board.session = s
    asyncio.run(s._speak("hello there", then_listen=False))
    return board


def test_slow_voice_start_never_overflows_the_board():
    board = speak(SlowThenFastTTS(seconds=3.0, first_byte_s=1.2))
    peak, gaps = board.replay()
    assert peak <= BOARD_BUFFER * 0.75, f"buffer reached {peak / BOARD_BUFFER:.0%}"
    assert gaps == 0


def test_all_the_audio_arrives_in_order_and_whole():
    tts = SlowThenFastTTS(seconds=1.3, first_byte_s=0.1)
    board = speak(tts)
    assert sum(n for _, n in board.arrivals) == len(tts.pcm)
    assert all(n % 2 == 0 for _, n in board.arrivals)  # whole samples only
