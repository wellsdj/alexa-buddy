"""The first switch-on of the day: a yellow fading light while Buddy gets the weather and its connections ready,
so the first questions are answered quickly. "hey buddy" must keep working while it runs."""
import asyncio
import time
import types
from datetime import datetime

from buddy import hub as hubmod


class _Board:
    def __init__(self, uptime=5):
        self.name, self.info, self.state, self.sent = "desk", {"uptime": uptime}, "idle", []

    async def send_json(self, msg):
        self.sent.append(msg)


def _hub(settings, weather_fails=False, slow_s=0.0):
    hub = hubmod.Hub.__new__(hubmod.Hub)
    hub.settings, hub.warmed, hub.warming = settings, False, False
    hub.startup_played, hub.device_gone_at = {}, {}
    hub.weather_ready, hub.events = (0.0, ""), []
    hub.brain = types.SimpleNamespace(now=lambda: datetime(2026, 11, 28, 8, 0))
    hub.record = lambda event, **data: hub.events.append((event, data))
    calls = []

    async def prepare_weather():
        calls.append("weather")
        await asyncio.sleep(slow_s)
        if not weather_fails:
            hub.weather_ready = (time.time(), "Sunny, 9 degrees.")

    async def warm():
        calls.append("claude")
    hub.prepare_weather = prepare_weather
    hub.llm = types.SimpleNamespace(warm=warm)
    hub.tts = types.SimpleNamespace(eleven=None)
    hub.spotify = types.SimpleNamespace(connected=False)
    hub.calls = calls
    return hub


def test_first_switch_on_gets_everything_ready_under_a_yellow_light(settings, monkeypatch):
    monkeypatch.setattr(hubmod, "WARM_UP_MIN_S", 0.05)
    hub, board = _hub(settings), _Board()
    assert hub.needs_warm_up(board)
    results = asyncio.run(hub.warm_up_day(board))
    assert set(hub.calls) == {"weather", "claude"} and results["weather"] == "ok"
    assert board.sent[0] == {"type": "warming", "on": True} and board.sent[-1] == {"type": "warming", "on": False}
    assert board.state == "idle"  # its own light, not a state: "hey buddy" still listens while it runs
    assert hub.events[-1][0] == "warm_up"
    # Once a day: switching on again later the same day just starts (no second warm-up).
    assert not hub.needs_warm_up(_Board(uptime=3))
    # A quick Wi-Fi reconnect never warms up again either.
    assert not hub.needs_warm_up(_Board(uptime=4000))


def test_next_day_warms_up_again(settings, monkeypatch):
    monkeypatch.setattr(hubmod, "WARM_UP_MIN_S", 0.0)
    hub = _hub(settings)
    asyncio.run(hub.warm_up_day(_Board()))
    hub.brain.now = lambda: datetime(2026, 11, 29, 7, 30)
    assert hub.needs_warm_up(_Board(uptime=3))


def test_light_goes_off_even_when_something_fails(settings, monkeypatch):
    monkeypatch.setattr(hubmod, "WARM_UP_MIN_S", 0.0)
    hub, board = _hub(settings, weather_fails=True), _Board()

    async def broken():
        raise RuntimeError("no internet")
    hub.llm.warm = broken
    results = asyncio.run(hub.warm_up_day(board))
    assert results["weather"] == "not ready" and results["claude"].startswith("failed")
    assert board.sent[-1] == {"type": "warming", "on": False} and not hub.warming


def test_lasts_as_long_as_it_needs(settings, monkeypatch):
    monkeypatch.setattr(hubmod, "WARM_UP_MIN_S", 0.0)
    hub, board = _hub(settings, slow_s=0.3), _Board()
    t = time.monotonic()
    asyncio.run(hub.warm_up_day(board))
    assert time.monotonic() - t >= 0.3 and board.sent[-1] == {"type": "warming", "on": False}
