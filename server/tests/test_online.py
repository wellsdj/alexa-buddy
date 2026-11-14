"""Going online: a real browser on a pretend booking site. Claude's moves are scripted."""
import asyncio
import json
from pathlib import Path

import pytest

from buddy.online import Browser, OnlineAgent

pw = pytest.importorskip("playwright")

SITE = """<html><title>Court booking</title><body>
<h1>Tennis courts</h1>
<label>Password <input type=password name=pw></label>
<select id=time><option>17:00</option><option>18:00</option></select>
<button onclick="document.getElementById('msg').innerText='Booked court 2 at '+document.getElementById('time').value">
Book court 2 (£8)</button>
<p id=msg></p></body></html>"""


class Script:
    """Pretend Claude: plays back tool calls in order."""
    def __init__(self, calls):
        self.calls = calls
        self.seen = []

    async def chat(self, messages, **kw):
        from types import SimpleNamespace
        self.seen.append(messages[-1].get("content", ""))
        name, args = self.calls.pop(0)
        call = {"id": f"t{len(self.seen)}", "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}
        return SimpleNamespace(tool_calls=[call], content="",
                               assistant_message={"role": "assistant", "content": "", "tool_calls": [call]})


def run_job(tmp_path, calls, answers):
    site = tmp_path / "site.html"
    site.write_text(SITE)
    llm = Script(calls)
    agent = OnlineAgent({"online_on": True}, llm, Browser(tmp_path / "profile", visible=False))
    said = []

    async def say(text, listen):
        said.append(text)
        if listen:
            asyncio.get_running_loop().call_later(0.05, agent.answer, answers.pop(0))

    agent.say = say

    async def go():
        try:
            await agent.browser.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser here: {str(e)[:80]}")
        agent.start("book a tennis court at 6", today="Saturday")
        await agent.job.task_handle
        page = await agent.browser.page.inner_text("#msg", timeout=2000)
        await agent.browser.close()
        return page
    return asyncio.run(go()), said, llm, agent


def test_books_only_after_a_yes_and_never_types_passwords(tmp_path):
    url = (tmp_path / "site.html").as_uri()
    calls = [("open_url", {"url": url}), ("type_text", {"n": 1, "text": "hunter2"}),
             ("choose", {"n": 2, "option": "18:00"}), ("click", {"n": 3}),
             ("ask_user", {"question": "Court 2 at 6pm for £8. Shall I book it?", "final": True}),
             ("click", {"n": 3}), ("look", {}), ("finish", {"summary": "Booked court 2 at 6pm, £8."})]
    msg, said, llm, agent = run_job(tmp_path, calls, ["yes please"])
    assert any("Never type passwords" in s for s in llm.seen)
    assert any("finishes something for real" in s for s in llm.seen)  # the first click was refused
    assert msg == "Booked court 2 at 18:00"
    assert said == ["Court 2 at 6pm for £8. Shall I book it?", "Booked court 2 at 6pm, £8."]
    assert agent.job.status == "done"


def test_a_no_means_nothing_is_booked(tmp_path):
    url = (tmp_path / "site.html").as_uri()
    calls = [("open_url", {"url": url}),
             ("ask_user", {"question": "Book court 2 at 5pm for £8?", "final": True}),
             ("click", {"n": 3}), ("finish", {"summary": "Okay, I didn't book anything.", "success": False})]
    msg, said, llm, agent = run_job(tmp_path, calls, ["no, not yet"])
    assert msg == "" and "NOT a yes" in llm.seen[2] and "finishes something" in llm.seen[3]
