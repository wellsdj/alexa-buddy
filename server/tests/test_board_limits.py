"""The server warns when the desk unit gets close to its memory, CPU or storage limits (firmware/LIMITS.md)."""
from buddy import board_limits
from buddy.hub import Hub

HEALTHY = {"heap": 110_000, "min_heap": 90_000, "largest": 60_000, "psram": True, "psram_free": 7_000_000,
           "mem_fallbacks": 0, "stack_min": 2400, "stack_task": "play", "loop_max_ms": 12, "app_kb": 1322,
           "app_slot_kb": 4096, "nvs_free": 300, "sd": True, "sd_free_mb": 29_000,
           "dropped_mic": 3, "dropped_spk": 0, "dropped_rec": 0,
           "cpu0": 7, "cpu1": 24, "cpu_top": "loopTask", "cpu_top_pct": 21, "mic_max_us": 1064, "aec_skipped": 0}


def whats(warnings):
    return " | ".join(w["what"] for w in warnings)


def test_healthy_board_has_no_warnings():
    assert board_limits.check(HEALTHY, HEALTHY) == []


def test_the_1_7_10_sd_card_numbers_are_caught():
    # measured on the board with the SD card in, firmware 1.7.10: 17 KB free after hello, lowest 1 KB
    info = {**HEALTHY, "heap": 17 * 1024, "min_heap": 1024, "largest": 7 * 1024, "psram": False}
    w = board_limits.check(info)
    text = whats(w)
    assert "internal RAM is low: 17 KB" in text and "dipped to 1 KB" in text and "PSRAM isn't working" in text
    assert all(x["fix"] for x in w) and any(x["level"] == "high" for x in w)


def test_cpu_stack_and_storage_warnings():
    info = {**HEALTHY, "stack_min": 600, "stack_task": "rec", "loop_max_ms": 900, "app_kb": 3600,
            "sd_free_mb": 300, "nvs_free": 10}
    text = whats(board_limits.check(info))
    for bit in ("rec task has only 600 bytes", "main loop stalled for 900 ms", "87% of its 4096 KB slot",
                "SD card has 300 MB free", "settings store is nearly full"):
        assert bit in text


def test_cpu_warnings():
    info = {**HEALTHY, "cpu0": 97, "cpu_top": "mic", "mic_max_us": 18_500, "aec_skipped": 4}
    text = whats(board_limits.check(info, HEALTHY))
    assert "core 0 is 97% busy (busiest task: mic)" in text
    assert "took 18.5 ms" in text and "echo cancelling was switched off" in text


def test_dropped_audio_between_stats():
    prev = dict(HEALTHY)
    now = {**HEALTHY, "dropped_mic": 3 + 200}
    assert "200 pieces of microphone audio" in whats(board_limits.check(now, prev))
    assert board_limits.check(now, None) == []  # no earlier stats to compare with


def test_no_sd_card_is_not_a_storage_warning():
    assert board_limits.check({**HEALTHY, "sd": False, "sd_free_mb": -1}) == []


class FakeSession:
    def __init__(self, info):
        self.info, self.name = info, "buddy-desk"


def test_hub_records_warnings_once_and_shows_them():
    hub = Hub.__new__(Hub)
    hub.recorded = []
    hub.record = lambda ev, **d: hub.recorded.append((ev, d))
    s = FakeSession({**HEALTHY, "heap": 20_000})
    hub.devices = lambda: [s]
    hub.check_board_limits(s, None)
    hub.check_board_limits(s, None)  # same problem again: not recorded twice
    assert [e for e, _ in hub.recorded] == ["board_limits"]
    health = hub.board_health()
    assert not health["ok"] and health["devices"][0]["measured"]["internal_free_kb"] == 19
    assert "internal RAM is low" in health["devices"][0]["warnings"][0]["what"]


def test_board_health_without_a_board():
    hub = Hub.__new__(Hub)
    hub.devices = lambda: []
    assert hub.board_health()["ok"] is False


def test_stuck_audio_task_is_a_warning():
    w = board_limits.check({**HEALTHY, "heartbeat": 0})
    assert w[0]["level"] == "high" and "stopped checking in" in w[0]["what"]
    assert board_limits.check({**HEALTHY, "heartbeat": 1}) == []


def test_heartbeat_setting_goes_to_the_board():
    import asyncio

    sent = []

    class Dev:
        role, id, info = "device", "d1", {}

        async def send_json(self, msg):
            sent.append(msg)

    hub = Hub.__new__(Hub)
    hub.settings = {"heartbeat_light": False}
    hub.broadcast = lambda msg, role=None, exclude=None: _collect(sent, msg)
    hub._soon = lambda coro: asyncio.run(coro)
    hub._settings_changed({"heartbeat_light": False})
    assert {"type": "heartbeat", "on": False} in sent


async def _collect(sent, msg):
    sent.append(msg)


def test_heartbeat_is_on_by_default_and_tunable():
    from buddy.config import DEFAULTS
    from buddy.tuning import SETTABLE

    assert DEFAULTS["heartbeat_light"] is True and "heartbeat_light" in SETTABLE
