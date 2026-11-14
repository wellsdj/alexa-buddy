"""Changing the desk unit's Wi-Fi by voice, spelling the password a character at a time."""
import asyncio
import json

import numpy as np

from tests.test_quiet_wake import setup
from tests.fakes import claude_message, claude_text, claude_tool


def wifi_hub(settings, claude):
    hub, s, ws, spoken = setup(settings, "")
    hub.stt.apis.transcripts.clear()
    hub.llm._http = claude.client()
    hub.llm._client = None

    async def device_send(msg):
        ws.sent.append(msg)
        if msg.get("type") == "wifi_scan":
            hub.wifi_list_arrived({"type": "wifi_list", "current": "Home", "networks": [
                {"ssid": "Home", "rssi": -40, "open": False}, {"ssid": "Sams House", "rssi": -60, "open": False},
                {"ssid": "Cafe Free", "rssi": -70, "open": True}]})
    s.send_json = device_send
    return hub, s, ws, spoken


def say(hub, s, text):
    hub.stt.apis.transcripts.append(text)
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))


def test_spelling_a_password_then_joining(settings, claude, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _fast_sleep)
    hub, s, ws, spoken = wifi_hub(settings, claude)
    claude.script = [claude_message(claude_tool("wifi", {"action": "scan"})),
                     claude_message(claude_text("I can see Home, Sams House and Cafe Free. Which one?")),
                     claude_message(claude_tool("wifi", {"action": "join", "ssid": "sams house"}))]
    say(hub, s, "can you change the wifi you're on")
    assert "Which one?" in spoken[-1]
    say(hub, s, "Sams House")
    assert spoken[-1].startswith("Sams House needs a password")
    for part in ["capital D", "small o", "dee", "number 3"]:
        say(hub, s, part)
    assert spoken[-1] == "Yes, number 3. Next?"
    say(hub, s, "done")
    assert "capital D, small o, small d, number 3" in spoken[-1]
    say(hub, s, "the small d should be a capital D")
    assert "capital D, small o, capital D, number 3" in spoken[-1]
    say(hub, s, "yes")
    assert spoken[-1].startswith("Okay, joining Sams House")
    join = next(m for m in ws.sent if m.get("type") == "wifi_join")
    assert join == {"type": "wifi_join", "ssid": "Sams House", "pass": "DoD3"}
    # The password never went to Claude, into the conversation, or into the log.
    everything = json.dumps(claude.requests) + json.dumps(hub.conv.history) + json.dumps(list(hub.log), default=str)
    assert "DoD3" not in everything and "small o" not in everything and "capital D" not in everything


def test_an_open_network_joins_straight_away(settings, claude, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _fast_sleep)
    hub, s, ws, spoken = wifi_hub(settings, claude)
    claude.script = [claude_message(claude_tool("wifi", {"action": "join", "ssid": "Cafe Free"}))]
    say(hub, s, "join the cafe wifi")
    assert spoken[-1].startswith("Okay, joining Cafe Free")
    assert {"type": "wifi_join", "ssid": "Cafe Free", "pass": ""} in ws.sent


def test_cancel_leaves_the_wifi_alone(settings, claude, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _fast_sleep)
    hub, s, ws, spoken = wifi_hub(settings, claude)
    claude.script = [claude_message(claude_tool("wifi", {"action": "join", "ssid": "Sams House"}))]
    say(hub, s, "join sams house")
    say(hub, s, "capital A")
    say(hub, s, "never mind")
    assert spoken[-1] == "Okay, I'll stay on the Wi-Fi I'm on." and hub.conv.pending is None
    assert not any(m.get("type") == "wifi_join" for m in ws.sent)


def test_it_says_how_the_change_went_when_its_back(settings):
    hub, s, ws, spoken = setup(settings, "")

    async def run():
        await hub.wifi_join_result(s, {"wifi_join": "failed", "wifi_join_ssid": "Sams House", "ssid": "Home"})
    asyncio.run(run())
    assert "couldn't join Sams House" in spoken[-1] and "back to Home" in spoken[-1]


_real_sleep = asyncio.sleep


async def _fast_sleep(t, *a, **k):
    await _real_sleep(0)
