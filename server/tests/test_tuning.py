"""Buddy looking at and changing its own settings, and offering help when hearing goes wrong."""
import asyncio
import json

import numpy as np

from buddy.brain import Conversation
from buddy.tuning import SETTABLE, change, describe
from tests.fakes import claude_message, claude_text, claude_tool
from tests.test_llm_brain import make_brain


def test_safe_changes_only(settings):
    assert change(settings, "end_of_speech_ms", "900") == (True, "Changed end_of_speech_ms (how long a pause means "
                                                                  "they've finished talking, in ms (longer: fewer "
                                                                  "cut-offs but slower answers)) from 600 to 900.")
    ok, note = change(settings, "end_of_speech_ms", "99999")
    assert ok and settings["end_of_speech_ms"] == 2000 and "outside the safe range" in note
    assert change(settings, "sound_effects", "off")[0] and settings["sound_effects"] is False
    assert not change(settings, "follow_up", "sometimes")[0]  # not one of the choices
    assert not change(settings, "device_token", "abc")[0]  # secrets and tokens can't be touched
    assert not change(settings, "ANTHROPIC_API_KEY", "x")[0]


def test_nothing_secret_is_shown(settings):
    settings.set_secret("ANTHROPIC_API_KEY", "sk-ant-secret")
    text = describe(settings)
    assert "sk-ant" not in text and "device_token" not in text and "end_of_speech_ms = 600" in text
    assert all(name in text for name in SETTABLE)


def test_buddy_changes_a_setting_when_asked(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    claude.script = [claude_message(claude_tool("change_setting", {"name": "voice_speed", "value": "0.9"})),
                     claude_message(claude_text("Done, I'll talk a bit slower."))]
    r = asyncio.run(brain.handle("can you talk a bit slower", Conversation()))
    assert settings["voice_speed"] == 0.9 and r.text == "Done, I'll talk a bit slower."
    result = claude.requests[1]["messages"][-1]["content"][0]["content"]
    assert "from 1.0 to 0.9" in result


def test_buddy_can_see_how_its_doing(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    brain.insight = lambda: "Answer speed: average of the last 10 1.2 s."
    claude.script = [claude_message(claude_tool("get_settings", {})),
                     claude_message(claude_text("About 1.2 seconds on average."))]
    asyncio.run(brain.handle("how fast are you answering", Conversation()))
    result = claude.requests[1]["messages"][-1]["content"][0]["content"]
    assert "1.2 s" in result and "end_of_speech_ms = 600" in result


def test_after_trouble_hearing_it_offers_help_once(settings, claude):
    from tests.test_streaming import streamed_hub, StreamingClaude
    c = StreamingClaude()
    hub, s, board, apis = streamed_hub(settings, c)
    settings.update({"stream_replies": False})
    import time
    hub.trouble.extend([(time.time(), "not_heard"), (time.time(), "not_heard")])
    c.script = [claude_message(claude_text("Paris.")), claude_message(claude_text("Rome."))]
    apis.transcripts = ["what's the capital of France", "and Italy"]
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    system = json.dumps(c.requests[0]["system"])
    assert "Worth offering once" in system and "listen more carefully" in system
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    assert "Worth offering once" not in json.dumps(c.requests[1]["system"])  # not again


def test_asking_what_it_said_twice_gets_an_offer_to_speak_up(settings):
    from tests.test_quiet_wake import setup
    hub, s, ws, spoken = setup(settings, "")
    hub.stt.apis.transcripts.clear()
    hub.conv.last_reply = "It's sunny today."
    for _ in range(2):
        hub.stt.apis.transcripts.append("what did you say")
        asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    assert "speak up" not in spoken[0] and "want me to speak up a bit" in spoken[1]
    hub.stt.apis.transcripts.append("what did you say")
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0))
    assert "speak up" not in spoken[2]  # only offered once
