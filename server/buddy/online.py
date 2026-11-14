"""Buddy going online to do things for you: booking a tennis court, checking an order, filling a form.

A real browser runs on this computer (Chrome-style, through Playwright). Claude reads each page as
text with numbered buttons and boxes, and picks what to click or type, one step at a time. It runs in
the background while you carry on, and Buddy speaks up when it needs you or when it's finished.

Safety, enforced here in code (not just asked of Claude):
- Anything final (book, pay, confirm, buy, submit, send...) needs a "yes" from you first, spoken.
- It never types passwords or card numbers. When a site needs you to log in, Buddy asks you to do
  it in the browser window on the Mac; the login is then remembered for next time.
- At most 60 steps and 15 minutes per job, and you can say "stop" at any time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("buddy.online")

MAX_STEPS = 60
MAX_SECONDS = 15 * 60
PAGE_TEXT = 6000  # characters of page text Claude sees per look

# Clicking one of these finishes something for real: it needs a spoken yes first.
FINAL = re.compile(r"\b(book|booking|reserve|confirm|pay|payment|purchase|buy|order|checkout|check out|submit|"
                   r"send|place|complete|finish|register|sign up|subscribe|delete|remove|cancel|agree|accept)\b", re.I)
CARD = re.compile(r"card|cvv|cvc|cc-|security code|expiry|sort code|account number|iban", re.I)

# Marks every visible thing you can click or type into with a number, and returns the page as text.
SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-buddy-n]').forEach(e => e.removeAttribute('data-buddy-n'));
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox], ' +
              '[role=radio], [role=tab], [role=option], [role=menuitem], [onclick], [contenteditable=true]';
  const items = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    if (r.width < 2 || r.height < 2 || st.visibility === 'hidden' || st.display === 'none') continue;
    if (el.type === 'hidden') continue;
    n += 1;
    el.setAttribute('data-buddy-n', String(n));
    const tag = el.tagName.toLowerCase();
    const secret = el.type === 'password' || /cc-|card|cvv|cvc/i.test(el.autocomplete || el.name || '');
    const label = (el.getAttribute('aria-label') || el.innerText || (secret ? (el.value ? '(filled in)' : '') : el.value) || el.placeholder ||
                   el.title || el.name || el.id || '').replace(/\s+/g, ' ').trim().slice(0, 80);
    let kind = el.getAttribute('role') || tag;
    if (tag === 'input') kind = 'input:' + (el.type || 'text');
    let extra = '';
    if (tag === 'select') extra = ' options: ' + [...el.options].slice(0, 25).map(o => o.text.trim()).join(' | ');
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') extra += ' (disabled)';
    if (el.checked) extra += ' (ticked)';
    const meta = [el.type, el.name, el.id, el.autocomplete, el.placeholder].filter(Boolean).join(' ');
    items.push({n, kind, label, extra, meta});
  }
  const text = (document.body ? document.body.innerText : '').replace(/\n\s*\n+/g, '\n');
  return {title: document.title, url: location.href, text, items};
}
"""


@dataclass
class Job:
    task: str
    who: str = ""
    started: float = field(default_factory=time.time)
    status: str = "working"  # working, waiting (for you), done, failed, stopped
    steps: list[str] = field(default_factory=list)
    question: str = ""
    result: str = ""
    allowed_final: int = 0  # final clicks you've said yes to
    answer: asyncio.Future | None = None
    task_handle: asyncio.Task | None = None


class Browser:
    """One real browser, kept between jobs so logins are remembered (data/browser)."""

    def __init__(self, profile: Path, visible: bool = True):
        self.profile = profile
        self.visible = visible
        self._pw = None
        self._ctx = None
        self.page = None
        self.items: dict[int, dict] = {}

    async def start(self) -> None:
        if self.page is not None and not self.page.is_closed():
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError("the web browser part isn't installed yet: double-click Update Buddy") from e
        if self._pw is None:
            self._pw = await async_playwright().start()
        if self._ctx is None:
            self.profile.mkdir(parents=True, exist_ok=True)
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(self.profile), headless=not self.visible, viewport={"width": 1280, "height": 860},
                executable_path=os.environ.get("BUDDY_CHROMIUM") or None)
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    async def close(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = self._pw = self.page = None

    async def open(self, url: str) -> None:
        await self.start()
        if not re.match(r"(https?|file)://", url):
            url = "https://" + url
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self._settle()

    async def _settle(self) -> None:
        try:
            await self.page.wait_for_load_state("networkidle", timeout=4000)
        except Exception:  # noqa: BLE001 - busy pages never go idle; carry on
            pass

    async def look(self) -> str:
        await self.start()
        snap = await self.page.evaluate(SNAPSHOT_JS)
        self.items = {i["n"]: i for i in snap["items"]}
        lines = [f'{i["n"]}. [{i["kind"]}] {i["label"]}{i["extra"]}' for i in snap["items"][:250]]
        text = snap["text"]
        if len(text) > PAGE_TEXT:
            text = text[:PAGE_TEXT] + "\n... (more below: scroll to see it)"
        return (f"Page: {snap['title']}\nAddress: {snap['url']}\n\nWhat's on the page:\n{text}\n\n"
                f"Things you can click or type into:\n" + "\n".join(lines))

    def item(self, n: int) -> dict | None:
        return self.items.get(int(n))

    def _el(self, n: int):
        return self.page.locator(f'[data-buddy-n="{int(n)}"]').first

    async def click(self, n: int) -> None:
        """Click like a person; if something's in the way, scroll to it and try harder."""
        el = self._el(n)
        try:
            await el.click(timeout=5000)
        except Exception:  # noqa: BLE001 - covered by a banner, off screen, animating...
            try:
                await el.scroll_into_view_if_needed(timeout=3000)
                box = await el.bounding_box()
                if box:
                    await self.page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                else:
                    await el.click(timeout=5000, force=True)
            except Exception:  # noqa: BLE001
                await el.evaluate("e => e.click()")
        await self._settle()

    async def type(self, n: int, text: str, enter: bool = False) -> None:
        el = self._el(n)
        await el.fill(text, timeout=8000)
        if enter:
            await el.press("Enter")
            await self._settle()

    async def choose(self, n: int, option: str) -> None:
        await self._el(n).select_option(label=option, timeout=8000)
        await self._settle()

    async def scroll(self, down: bool = True) -> None:
        await self.page.mouse.wheel(0, 700 if down else -700)
        await asyncio.sleep(0.4)

    async def back(self) -> None:
        await self.page.go_back(timeout=15000)
        await self._settle()


TOOLS = [
    {"type": "function", "function": {"name": "open_url", "description": "Go to a web address.",
     "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "look", "description": "Read the page again (after waiting or scrolling).",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "click", "description": "Click numbered thing n from the latest look.",
     "parameters": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}}},
    {"type": "function", "function": {"name": "type_text", "description": "Type into box n (replaces what's there). "
     "enter=true presses Enter after (e.g. for a search box).",
     "parameters": {"type": "object", "properties": {"n": {"type": "integer"}, "text": {"type": "string"},
                                                     "enter": {"type": "boolean"}}, "required": ["n", "text"]}}},
    {"type": "function", "function": {"name": "fill_login", "description": "Log in with the user's saved login for "
     "this site: fills box user_n with their username/email and box password_n with their password (you never see "
     "it). Then click the log in button yourself.",
     "parameters": {"type": "object", "properties": {"user_n": {"type": "integer"}, "password_n": {"type": "integer"}},
                    "required": ["password_n"]}}},
    {"type": "function", "function": {"name": "choose", "description": "Pick an option in drop-down n.",
     "parameters": {"type": "object", "properties": {"n": {"type": "integer"}, "option": {"type": "string"}},
                    "required": ["n", "option"]}}},
    {"type": "function", "function": {"name": "scroll", "description": "Scroll the page down (or up).",
     "parameters": {"type": "object", "properties": {"up": {"type": "boolean"}}}}},
    {"type": "function", "function": {"name": "go_back", "description": "The browser's back button.",
     "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "ask_user", "description": "Ask the user something out loud and wait for "
     "their answer: a choice between options, a missing detail, asking them to log in on the Mac, or permission "
     "before anything final. For permission, set final=true and say exactly what will happen (what, when, where, "
     "price). Keep it short and spoken: one or two sentences.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}, "final": {"type": "boolean"}},
                    "required": ["question"]}}},
    {"type": "function", "function": {"name": "finish", "description": "The job is done (or can't be done). summary: "
     "one or two spoken sentences for the user saying what happened, with the important details.",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "success": {"type": "boolean"}},
                    "required": ["summary"]}}},
]

SYSTEM = """You are Buddy's hands on the web. You control a real browser on the user's computer to get a job \
done for them, step by step. After each step you see the page as text with numbered things to click or type into.

The job and the user's answers come from speech-to-text, so words may be mis-heard: read them by sound and \
context ("club spark" is ClubSpark, "Lockhart" when logging in is "login").

Finding the site:
- The request comes from speech-to-text, so addresses arrive as words: "studdy buddy dot vercel dot app slash \
code" means studdy-buddy.vercel.app/code (try the obvious spellings, with and without dashes).
- They may only give a name ("ClubSpark", "the council tennis courts", "Pizza Express") or no site at all. Work out \
the site yourself: if you know it, go there (clubspark.lta.org.uk, not "clubspark" alone); otherwise search \
(https://duckduckgo.com/html/?q=... or https://www.google.com/search?q=...) and pick the official result. Add \
their area to the search when it's about somewhere local.
- Never give up because an address was vague or didn't load: try the other spelling, search for it, try another \
search engine, or a different page on the site.

How to work:
- Prefer the site's own buttons and forms. If something doesn't work, look again, scroll, or try another way.
- Be persistent: keep going until the job is really done. Only finish without success after several genuinely \
different attempts, and then say exactly what you tried and what stopped you.
- Choose sensible defaults from what the user said; ask_user only when a choice really matters to them \
(e.g. which of several times) or something is missing.
- Anything that commits them (booking, paying, ordering, sending, signing up, cancelling) needs ask_user with \
final=true first, saying exactly what will happen and the price. Only after a yes, click it.
- Logging in: use fill_login (their saved login for that site). If there isn't one saved, use ask_user to ask \
them to log in themselves in the browser window on their Mac (or save the login in Buddy's settings under \
Website logins) and say "done". Logins stay remembered in this browser afterwards.
- Never type passwords or card details yourself. If payment details are needed, ask them to fill those in \
themselves in the browser window.
- Don't accept extra paid add-ons, insurance or newsletters unless asked.
- When finished, call finish with a short spoken summary (what, when, where, price, reference number if any).
Today is {today}. The user's home: {home}. Who asked: {who}."""


def _args(call: dict) -> dict:
    try:
        return json.loads(call["function"].get("arguments") or "{}")
    except json.JSONDecodeError:
        return {}


class OnlineAgent:
    def __init__(self, settings, llm, browser: Browser | None = None):
        self.settings = settings
        self.llm = llm
        self.browser = browser or Browser(Path(settings.data_dir) / "browser",
                                          visible=bool(settings.get("online_show_browser", True)))
        self.job: Job | None = None
        self.say = None  # async (text, wait_for_answer: bool): the hub speaks it out loud
        self.logins = None  # LoginStore: saved website logins (passwords never go to Claude)
        self.on_busy = None  # (bool): a job started / ended (the board shows a slow pink light)
        self.history: list[Job] = []

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.status in ("working", "waiting")

    @property
    def waiting(self) -> bool:
        return self.job is not None and self.job.status == "waiting"

    def start(self, task: str, who: str = "", today: str = "", home: str = "") -> str:
        if not self.settings.get("online_on", True):
            return "Going online to do things is switched off in Settings (online_on)."
        if self.busy:
            return f"Already busy with: {self.job.task}. Say stop first if you want something else."
        self.job = Job(task=task, who=who)
        self.job.task_handle = asyncio.create_task(self._run(self.job, today, home))
        if self.on_busy:
            self.on_busy(True)
        return "started"

    def answer(self, text: str) -> None:
        """The user's spoken answer to the question the job asked."""
        j = self.job
        if j and j.answer and not j.answer.done():
            j.answer.set_result(text)

    def stop(self) -> str:
        j = self.job
        if not self.busy:
            return "nothing running"
        j.status = "stopped"
        if j.answer and not j.answer.done():
            j.answer.cancel()
        if j.task_handle:
            j.task_handle.cancel()
        return f"stopped: {j.task}"

    def status(self) -> str:
        j = self.job
        if not j:
            return "Nothing done online yet."
        last = "; ".join(j.steps[-4:])
        mins = int((time.time() - j.started) / 60)
        s = f"Job: {j.task}. Status: {j.status}, {len(j.steps)} steps, {mins} min. Latest: {last}."
        if j.status == "waiting":
            s += f" Waiting for the user to answer: {j.question}"
        if j.result:
            s += f" Result: {j.result}"
        return s

    async def _speak(self, text: str, listen: bool = False) -> None:
        if self.say:
            await self.say(text, listen)

    async def _ask(self, job: Job, question: str) -> str:
        job.status, job.question = "waiting", question
        job.answer = asyncio.get_running_loop().create_future()
        await self._speak(question, True)
        try:
            return await asyncio.wait_for(job.answer, timeout=10 * 60)
        except asyncio.TimeoutError:
            return ""
        finally:
            job.status, job.question = "working", ""

    async def _run(self, job: Job, today: str, home: str) -> None:
        try:
            await asyncio.wait_for(self._loop(job, today, home), timeout=MAX_SECONDS)
        except asyncio.CancelledError:
            job.status = "stopped"
        except asyncio.TimeoutError:
            job.status, job.result = "failed", "it was taking too long"
            await self._speak(f"Sorry, I gave up on {job.task}: it was taking too long. Nothing was booked or paid.")
        except Exception as e:  # noqa: BLE001
            log.exception("online job failed")
            job.status, job.result = "failed", str(e)
            await self._speak(f"Sorry, something went wrong online, so I stopped: {str(e)[:120]}")
        finally:
            self.history = (self.history + [job])[-20:]
            if self.on_busy:
                self.on_busy(False)

    async def _loop(self, job: Job, today: str, home: str) -> None:
        system = SYSTEM.format(today=today or time.strftime("%A %d %B %Y"), home=home or "unknown",
                               who=job.who or "the user")
        messages: list[dict] = [{"role": "system", "content": system},
                                {"role": "user", "content": f"The job: {job.task}"}]
        for _ in range(MAX_STEPS):
            if job.status == "stopped":
                return
            res = await self.llm.chat(messages, tier="smart", tools=TOOLS, max_tokens=1200)
            if not res.tool_calls:
                messages.append({"role": "assistant", "content": res.content or "..."})
                messages.append({"role": "user", "content": "Use the tools: carry on with the job, or call finish."})
                continue
            messages.append(res.assistant_message)
            for call in res.tool_calls:
                name, a = call["function"]["name"], _args(call)
                result = await self._tool(job, name, a)
                if job.status in ("done", "stopped"):
                    return
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
            self._trim(messages)
        job.status, job.result = "failed", "too many steps"
        await self._speak(f"I couldn't finish {job.task}: it needed too many steps. Nothing was booked or paid.")

    @staticmethod
    def _trim(messages: list[dict]) -> None:
        """Old page readings are long: keep only the last few in full."""
        seen = 0
        for m in reversed(messages):
            if m.get("role") == "tool" and len(m.get("content", "")) > 1500:
                seen += 1
                if seen > 3:
                    m["content"] = m["content"][:300] + "\n... (old page, trimmed)"

    async def _tool(self, job: Job, name: str, a: dict) -> str:
        b = self.browser
        try:
            if name == "open_url":
                job.steps.append(f"opened {a.get('url', '')}")
                await b.open(a.get("url", ""))
                return await b.look()
            if name == "look":
                return await b.look()
            if name == "scroll":
                await b.scroll(down=not a.get("up"))
                return await b.look()
            if name == "go_back":
                await b.back()
                return await b.look()
            if name == "click":
                it = b.item(a.get("n", 0))
                if not it:
                    return "No such number on the page: look again."
                ticks = it["kind"] in ("input:checkbox", "input:radio", "checkbox", "radio", "select", "textarea",
                                       "input:text", "input:search", "input:email", "input:date", "input:number")
                if not ticks and FINAL.search(it["label"]):
                    if job.allowed_final <= 0:
                        return (f'"{it["label"]}" looks like it finishes something for real. First use ask_user with '
                                "final=true saying exactly what will happen, and only click after they say yes.")
                    job.allowed_final -= 1
                job.steps.append(f"clicked {it['label'][:40]}")
                await b.click(it["n"])
                return await b.look()
            if name == "type_text":
                it = b.item(a.get("n", 0))
                if not it:
                    return "No such number on the page: look again."
                if "password" in it["kind"] or "password" in it["meta"].lower():
                    return "Don't type passwords: use fill_login for this box."
                if CARD.search(f"{it['label']} {it['meta']}"):
                    return ("Never type card or bank details. Use ask_user to ask them to fill those in themselves "
                            "in the browser window, then look again.")
                job.steps.append(f"typed into {it['label'][:30]}")
                await b.type(it["n"], str(a.get("text", "")), bool(a.get("enter")))
                return await b.look()
            if name == "fill_login":
                found = self.logins.find(b.page.url) if self.logins else None
                if not found:
                    return ("No saved login for this site. Use ask_user to ask them to log in themselves in the "
                            "browser window on the Mac (or save it in Buddy's settings, Website logins), then look again.")
                site, login = found
                pw = b.item(a.get("password_n", 0))
                if not pw or "password" not in f"{pw['kind']} {pw['meta']}".lower():
                    return "password_n isn't a password box: look again."
                if a.get("user_n"):
                    if not b.item(a["user_n"]):
                        return "user_n isn't on the page: look again."
                    await b.type(a["user_n"], login.get("username", ""))
                await b.type(pw["n"], login.get("password", ""))
                job.steps.append(f"filled in the saved login for {site}")
                return f"Filled in the saved login for {site} (username {login.get('username', '')}). Now click log in."
            if name == "choose":
                it = b.item(a.get("n", 0))
                if not it:
                    return "No such number on the page: look again."
                job.steps.append(f"chose {a.get('option', '')}")
                await b.choose(it["n"], str(a.get("option", "")))
                return await b.look()
            if name == "ask_user":
                q = str(a.get("question", "")).strip()
                job.steps.append(f"asked: {q[:60]}")
                reply = await self._ask(job, q)
                if not reply:
                    return "No answer (they may have walked away). Don't do anything final; finish and say so."
                if a.get("final") and YES.search(reply) and not NO.search(reply):
                    job.allowed_final += 1
                    return f'They said: "{reply}". That\'s a yes: go ahead with that one final step.'
                if a.get("final"):
                    return f'They said: "{reply}". That is NOT a yes: don\'t do the final step. Adjust or finish.'
                return f'They said: "{reply}"'
            if name == "finish":
                job.result = str(a.get("summary", "")).strip() or "Done."
                job.status = "done" if a.get("success", True) else "failed"
                job.steps.append("finished")
                await self._speak(job.result)
                return "ok"
        except Exception as e:  # noqa: BLE001 - a page error goes back to Claude to try another way
            return f"That didn't work: {str(e).splitlines()[0][:200]}. Look again and try another way."
        return f"unknown tool {name}"


YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead|do it|please do|ok|okay|book it|confirm|correct|that's right|"
                 r"sounds good|perfect|done|go for it)\b", re.I)
NO = re.compile(r"\b(no|nope|don't|do not|stop|wait|hold on|not yet|cancel|never mind)\b", re.I)
