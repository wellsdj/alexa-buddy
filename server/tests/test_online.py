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
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm, Browser(tmp_path / "profile", visible=False))
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


def test_books_only_after_a_yes(tmp_path):
    url = (tmp_path / "site.html").as_uri()
    calls = [("open_url", {"url": url}), ("type_text", {"n": 1, "text": "hunter2"}),
             ("choose", {"n": 2, "option": "18:00"}), ("click", {"n": 3}),
             ("ask_user", {"question": "Court 2 at 6pm for £8. Shall I book it?", "final": True}),
             ("click", {"n": 3}), ("look", {}), ("finish", {"summary": "Booked court 2 at 6pm, £8."})]
    msg, said, llm, agent = run_job(tmp_path, calls, ["yes please"])
    assert agent.job.typed_password == "hunter2" and "hunter2" not in " ".join(agent.job.steps)
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


LOGIN = """<html><title>Log in</title><body>
<input name=email placeholder=Email><input type=password name=pw>
<button onclick="document.getElementById('m').innerText='hi '+document.querySelector('[name=email]').value+
' '+(document.querySelector('[name=pw]').value=='s3cret!'?'ok':'bad')">Log in</button><p id=m></p></body></html>"""


def test_logs_in_with_a_saved_login_without_claude_seeing_the_password(tmp_path):
    from buddy.logins import LoginStore
    page = tmp_path / "login.html"
    page.write_text(LOGIN)
    store = LoginStore(tmp_path)
    store.save("localhost", "me@example.com", "s3cret!")
    calls = [("open_url", {"url": page.as_uri()}), ("fill_login", {"user_n": 1, "password_n": 2}),
             ("click", {"n": 3}), ("finish", {"summary": "Logged in."})]
    llm = Script(calls)
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm, Browser(tmp_path / "profile", visible=False))
    agent.logins = store
    agent.say = lambda t, l: asyncio.sleep(0)

    async def go():
        try:
            await agent.browser.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser here: {str(e)[:80]}")
        store.find = lambda url: ("example site", {"username": "me@example.com", "password": "s3cret!"})
        agent.start("log in")
        await agent.job.task_handle
        text = await agent.browser.page.inner_text("#m", timeout=2000)
        await agent.browser.close()
        return text
    assert asyncio.run(go()) == "hi me@example.com ok"
    assert not any("s3cret" in str(x) for x in llm.seen)  # Claude never saw it
    assert any("from the saved login for" in str(x) for x in llm.seen)


def test_a_login_you_give_is_saved_for_next_time(tmp_path):
    from buddy.logins import LoginStore
    page = tmp_path / "login.html"
    page.write_text(LOGIN)
    store = LoginStore(tmp_path)
    calls = [("open_url", {"url": page.as_uri()}), ("type_text", {"n": 1, "text": "me@school.org"}),
             ("type_text", {"n": 2, "text": "s3cret!"}), ("click", {"n": 3}),
             ("save_login", {"account": "School email"}), ("finish", {"summary": "Signed in."})]
    llm = Script(calls)
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm, Browser(tmp_path / "profile", visible=False))
    agent.logins = store
    agent.say = lambda t, l: asyncio.sleep(0)

    async def go():
        try:
            await agent.browser.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser here: {str(e)[:80]}")
        agent.start("sign in")
        await agent.job.task_handle
        await agent.browser.close()
    asyncio.run(go())
    assert store.account("school email")[1] == {"email": "me@school.org", "password": "s3cret!"}
    assert store.account("me@school.org")[0] == "school email"


def test_saved_logins_match_the_site():
    from buddy.logins import LoginStore
    import tempfile
    store = LoginStore(tempfile.mkdtemp())
    store.save("https://www.clubspark.lta.org.uk/somewhere", "me", "pw")
    assert store.find("https://login.clubspark.lta.org.uk/auth")[0] == "tennis"
    assert store.find("https://example.com") is None
    assert store.list() == [{"site": "tennis", "username": "me"}]


GOOGLE_PW = """<html><title>Hi Wells</title><body><h1>Hi Wells</h1><p>me@school.org</p>
<input type=password name=Passwd onkeydown="if(event.key=='Enter'){document.getElementById('m').innerText=
(this.value=='s3cret!'?'signed in':'wrong')}"><p id=m></p></body></html>"""


def test_google_password_page_uses_the_account_shown(tmp_path):
    from buddy.logins import LoginStore
    page = tmp_path / "pw.html"
    page.write_text(GOOGLE_PW)
    store = LoginStore(tmp_path)
    store.save_account("https://www.bromcomvle.com/", "me@school.org", "s3cret!")  # named after the site
    calls = [("open_url", {"url": page.as_uri()}), ("fill_login", {"password_n": 1}), ("finish", {"summary": "In."})]
    llm = Script(calls)
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm, Browser(tmp_path / "profile", visible=False))
    agent.logins = store
    agent.say = lambda t, l: asyncio.sleep(0)

    async def go():
        try:
            await agent.browser.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser here: {str(e)[:80]}")
        agent.start("sign in")
        await agent.job.task_handle
        text = await agent.browser.page.inner_text("#m", timeout=2000)
        await agent.browser.close()
        return text
    assert asyncio.run(go()) == "signed in"
    assert not any("s3cret" in str(x) for x in llm.seen)


GOOGLE_FLOW = """<html><body><div id=s1><p>Sign in</p><input type=email id=e
 onkeydown="if(event.key=='Enter'){s1.hidden=true;s2.hidden=false}"></div>
<div id=s2 hidden><p>Hi Wells</p><p>me@school.org</p><input type=password id=p
 onkeydown="if(event.key=='Enter'){s2.hidden=true;s3.hidden=false}"></div>
<div id=s3 hidden><button onclick="m.innerText='in as '+e.value">Continue as Wells</button></div><p id=m></p></body></html>"""


def test_google_sign_in_happens_in_one_go(tmp_path, monkeypatch):
    """No thinking between Google's steps: email, password, Continue, done straight after opening."""
    from buddy.logins import LoginStore
    page = tmp_path / "accounts.google.com.html"
    page.write_text(GOOGLE_FLOW)
    store = LoginStore(tmp_path)
    store.save_account("school email", "me@school.org", "s3cret!")
    calls = [("open_url", {"url": page.as_uri()}), ("finish", {"summary": "In."})]
    llm = Script(calls)
    agent = OnlineAgent({"online_on": True, "online_show_browser": False}, llm, Browser(tmp_path / "profile", visible=False))
    agent.logins = store
    agent.say = lambda t, l: asyncio.sleep(0)

    async def go():
        try:
            await agent.browser.start()
        except Exception as e:  # noqa: BLE001
            pytest.skip(f"no browser here: {str(e)[:80]}")
        orig = agent._autopilot

        async def pilot(job):  # the test page is a file, so pretend it's Google's address
            real_url = type(agent.browser.page).url
            monkeypatch.setattr(type(agent.browser.page), "url", property(lambda self: "https://accounts.google.com/x"))
            try:
                return await orig(job)
            finally:
                monkeypatch.setattr(type(agent.browser.page), "url", real_url)
        agent._autopilot = pilot
        agent.start("sign in", kind="school_homework")
        await agent.job.task_handle
        text = await agent.browser.page.inner_text("#m", timeout=2000)
        await agent.browser.close()
        return text
    assert asyncio.run(go()) == "in as me@school.org"
    assert len(llm.seen) == 2  # just: open it, then finish


def test_school_email_only_for_school_things(tmp_path):
    from buddy.logins import LoginStore
    store = LoginStore(tmp_path)
    store.save_account("https://www.bromcomvle.com/", "w@christs.school", "x")
    assert store.pick(school=True)[1]["email"] == "w@christs.school"
    assert store.pick(school=False) is None  # tennis must not use the school email
    store.save_account("home email", "me@gmail.com", "y")
    assert store.pick(school=False)[1]["email"] == "me@gmail.com"
    assert store.pick(school=True)[1]["email"] == "w@christs.school"


def test_tennis_goes_straight_to_clubspark(settings, apis, claude):
    from buddy.brain import Conversation
    from tests.fakes import claude_message, claude_tool
    from tests.test_llm_brain import make_brain
    brain = make_brain(settings, apis, claude)
    got = {}

    class O:
        busy = waiting = False
        def start(self, task, **kw):
            got.update(task=task, **kw)
            return "started"
    brain.online = O()
    claude.script = [claude_message(claude_tool("go_online", {"task": "book a tennis court Saturday 10am"}))]
    asyncio.run(brain.handle("book me a tennis court saturday at 10", Conversation()))
    assert got["kind"] == "tennis" and "clubspark.lta.org.uk" in got["task"] and "Richmond" in got["task"]


def test_one_tennis_login_for_lta_clubspark_and_pensford(tmp_path):
    from buddy.logins import LoginStore
    store = LoginStore(tmp_path)
    store.save("https://clubspark.lta.org.uk/PensfordTennisClub", "me@gmail.com", "pw")
    assert store.find("https://auth.lta.org.uk/login")[1]["username"] == "me@gmail.com"
    assert store.list() == [{"site": "tennis", "username": "me@gmail.com"}]
    store2 = LoginStore(tmp_path / "x")
    (tmp_path / "x").mkdir()
    store2.save_account("tennis", "me@gmail.com", "pw2")
    assert store2.find("https://clubspark.lta.org.uk/Booking")[1]["password"] == "pw2"
