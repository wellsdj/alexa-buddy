"""Taking down a website address by voice: read back, correct, or say it bit by bit."""
import asyncio

from buddy.brain import Conversation, address_from_speech, say_address
from tests.fakes import claude_message, claude_tool
from tests.test_llm_brain import make_brain


def test_spoken_addresses():
    assert address_from_speech("studdy dash buddy dot vercel dot app slash code") == "studdy-buddy.vercel.app/code"
    assert address_from_speech("go to www dot bbc dot co dot uk") == "www.bbc.co.uk"
    assert address_from_speech("https colon slash slash example dot com") == "example.com"
    assert address_from_speech("the smelly fish website") == ""  # not an address
    assert say_address("studdy-buddy.vercel.app/code") == "studdy, dash, buddy, dot, vercel, dot, app, slash, code"


def run(brain, conv, text):
    return asyncio.run(brain.handle(text, conv))


def test_read_back_then_yes_starts_the_job(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    started = []

    class Online:
        waiting = False
        def start(self, task, **kw):
            started.append(task)
            return "started"
    brain.online = Online()
    conv = Conversation()
    claude.script = [claude_message(claude_tool("check_address", {"heard": "studdy-buddy.vercel.app/code",
                                                                   "task": "get the code"}))]
    r = run(brain, conv, "go to studdy buddy dot vercel dot app slash code and get the code")
    assert "studdy, dash, buddy, dot, vercel, dot, app, slash, code" in r.text and r.text.endswith("?")
    r = run(brain, conv, "um yes, that's the one")  # (no Claude reply scripted: the word rules decide)
    assert started and "studdy-buddy.vercel.app/code" in started[0]
    assert conv.last_address == "studdy-buddy.vercel.app/code"


def test_no_then_bit_by_bit(settings, apis, claude):
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    conv.pending = {"intent": "url_confirm", "url": "study-buddy.vercel.app", "task": ""}
    r = run(brain, conv, "no")
    assert "a bit at a time" in r.text and r.follow_up
    run(brain, conv, "studdy dash buddy")
    run(brain, conv, "dot vercel dot app")
    r = run(brain, conv, "done")
    assert "studdy, dash, buddy, dot, vercel, dot, app" in r.text
    r = run(brain, conv, "yes")
    assert "remember" in r.text.lower()


def test_claude_works_out_what_they_meant(settings, apis, claude):
    from tests.fakes import claude_text
    brain = make_brain(settings, apis, claude)
    conv = Conversation()
    conv.pending = {"intent": "url_spell", "chars": "studdy-buddy.vercel.app", "task": ""}
    claude.script = [claude_message(claude_text('{"action": "other"}'))]
    r = run(brain, conv, "buddy that's right you can go look on the website now")
    assert conv.pending["chars"] == "studdy-buddy.vercel.app"  # chat isn't typed into the address
    claude.script = [claude_message(claude_text('{"action": "done"}'))]
    r = run(brain, conv, "that's all of it")
    assert r.text.endswith("Is that right?") and conv.pending["intent"] == "url_confirm"
