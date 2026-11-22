"""Echo cancelling: the board is told whether to take Buddy's own voice out of the mic."""
import asyncio

from buddy.hub import Hub, Session
from tests.test_alarm_pause import FakeWS


def test_board_is_told_on_connect_and_when_changed(settings):
    hub = Hub(settings)
    ws = FakeWS()
    s = Session(hub, ws, "device", "esp32")
    hub.sessions = {s.id: s}
    asyncio.run(hub.greet(s))
    assert {"type": "aec", "on": True} in ws.sent

    async def change():
        settings.update({"echo_cancel": False})
        await asyncio.sleep(0.05)
    asyncio.run(change())
    assert ws.sent[-1] == {"type": "aec", "on": False} or {"type": "aec", "on": False} in ws.sent


def test_the_startup_sound_plays_once_per_switch_on(settings):
    from buddy.hub import Hub, Session
    from tests.test_alarm_pause import FakeWS
    hub = Hub(settings)
    played = []
    for i in range(5):  # reconnecting over and over while starting up
        s = Session(hub, FakeWS(), "device", "esp32")
        s.info = {"uptime": 3 + i}
        async def snd(s=s):
            played.append(1)
        s.play_startup_sound = snd
        asyncio.run(hub.greet(s))
        import time as _t
        _t.sleep(1)
    assert len(played) == 1
