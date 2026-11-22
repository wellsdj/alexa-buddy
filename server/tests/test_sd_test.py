import asyncio

from buddy.hub import Hub


class FakeDev:
    def __init__(self, hub, reply):
        self.hub, self.reply, self.info = hub, reply, {"fw": "1.7.4"}

    async def send_json(self, msg):
        if msg["type"] == "sd_test" and self.reply:
            asyncio.get_running_loop().call_soon(self.hub.sd_test_arrived, {"type": "sd_test", **self.reply})


def _hub(reply):
    hub = Hub.__new__(Hub)
    hub._sd_wait = None
    hub.recorded = []
    hub.record = lambda ev, **d: hub.recorded.append((ev, d))
    dev = FakeDev(hub, reply)
    hub.devices = lambda: [dev]
    return hub


def test_sd_test_passes_result_through():
    hub = _hub({"ok": False, "steps": ["No answer from the card at all."], "hint": "Check the wires."})
    r = asyncio.run(hub.sd_test())
    assert r == {"ok": False, "steps": ["No answer from the card at all."], "hint": "Check the wires."}
    assert hub.recorded[0][0] == "sd_test"


def test_sd_test_no_device():
    hub = _hub(None)
    hub.devices = lambda: []
    r = asyncio.run(hub.sd_test())
    assert not r["ok"] and "isn't connected" in r["hint"]
