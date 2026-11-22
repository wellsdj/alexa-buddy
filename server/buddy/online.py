"""Buddy going online to do things for you: booking a tennis court, checking an order, filling a form.

A real browser runs on this computer (Chrome-style, through Playwright). Claude reads each page as
text with numbered buttons and boxes, and picks what to click or type, one step at a time. It runs in
the background while you carry on, and Buddy speaks up when it needs you or when it's finished.

Safety, enforced here in code (not just asked of Claude):
- Anything final (book, pay, confirm, buy, submit, send...) needs a "yes" from you first, spoken.
- Logins: it uses the ones saved on this Mac (per site, or your named accounts like "school email"),
  or asks you for them and saves them once signing in works. It never types card or bank details.
- At most 60 steps and 15 minutes per job, and you can say "stop" at any time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("buddy.online")

MAX_STEPS = 60
MAX_SECONDS = 15 * 60
PAGE_TEXT = 4500  # characters of page text Claude sees per look

# Clicking one of these finishes something for real: it needs a spoken yes first.
FINAL = re.compile(r"\b(book|booking|reserve|confirm|pay|payment|purchase|buy|order|checkout|check out|submit|"
                   r"send|place|complete|finish|register|sign up|subscribe|delete|remove|cancel|agree|accept)\b", re.I)
CARD = re.compile(r"card|cvv|cvc|cc-|security code|expiry|sort code|account number|iban", re.I)

# Marks every visible thing you can click or type into with a number, and returns the page as text.
SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-buddy-n]').forEach(e => e.removeAttribute('data-buddy-n'));
  const sel = 'a[href], button, input, select, textarea, [role=button], [role=link], [role=checkbox], ' +
              '[role=radio], [role=tab], [role=option], [role=menuitem], [onclick], [contenteditable=true], ' +
              '[class*="pencil"], [class*="fa-edit"], [class*="glyphicon"], [title]';
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
    // Icon-only buttons (a pencil to open homework, a bin, a cross): say what the icon is.
    const icon = [el, ...el.querySelectorAll('i, svg, span, img')].map(x => ((x.getAttribute && (x.getAttribute('class') || '')) + ' ' + (x.alt || '')))
      .join(' ').match(/(pencil|edit|eye|view|open|search|close|times|trash|bin|plus|download|upload|calendar|info)/i);
    if (icon) extra += ` (icon: ${icon[1].toLowerCase()})`;
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
    topic: str = ""
    typed_user: str = ""
    typed_password: str = ""
    kind: str = ""  # e.g. school_homework: the result is also remembered
    quiet: bool = False  # Buddy's own early-morning job: no light, no speaking, nothing asked
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
        self.chrome = False

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
            # Your real Google Chrome (not a bare test browser), with Buddy's own profile: sign in to your
            # Google account in it once and every site Buddy opens is signed in as you from then on.
            # Without the "being controlled" flags, Google lets you sign in.
            opts = dict(headless=not self.visible, viewport={"width": 1280, "height": 860},
                        # On the Mac, drop --no-sandbox (it only puts up a warning bar); Linux needs it.
                        ignore_default_args=["--enable-automation"] + (["--no-sandbox"] if sys.platform == "darwin" else []))
            exe = os.environ.get("BUDDY_CHROMIUM")
            try:
                if exe:
                    raise RuntimeError("test browser asked for")
                self._ctx = await self._pw.chromium.launch_persistent_context(str(self.profile), channel="chrome",
                                                                             **opts)
                self.chrome = True
            except Exception:  # noqa: BLE001 - Chrome not installed: the browser that came with Buddy
                self._ctx = await self._pw.chromium.launch_persistent_context(str(self.profile),
                                                                             executable_path=exe or None, **opts)
                self.chrome = False
        if not getattr(self, "_hid", False):
            # Sites like Google refuse sign-in in a browser that says it's automated; this hides that
            # without the warning bar a command-line flag puts up.
            await self._ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            self._hid = True
        self.page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()

    async def open_for_you(self, url: str = "https://accounts.google.com/") -> None:
        """Open the browser window for you (to sign in to Google or a site). It stays signed in."""
        self.visible = True
        await self.start()
        await self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self.page.bring_to_front()

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
            await self.page.wait_for_load_state("networkidle", timeout=1500)
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
    {"type": "function", "function": {"name": "fill_login", "description": "Fill in a saved login: the one saved "
     "for this site, or one of their accounts by name (account: e.g. \"school email\"). user_n: the email/username "
     "box, password_n: the password box (either or both: sign-ins often ask on two pages). Then click next/log in.",
     "parameters": {"type": "object", "properties": {"user_n": {"type": "integer"}, "password_n": {"type": "integer"},
                                                     "account": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "save_login", "description": "After signing in worked with details "
     "the user gave you, save them: for this site, and as one of their accounts if they named it (account: e.g. "
     "\"school email\"). Saves the email/username and password you typed in this job.",
     "parameters": {"type": "object", "properties": {"account": {"type": "string"}}}}},
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
     "what you found or did, spoken, with the important details (a few sentences at most). topic: what it was "
     "about in 2-4 words, e.g. \"your emails\" or \"the tennis courts\".",
     "parameters": {"type": "object", "properties": {"summary": {"type": "string"}, "topic": {"type": "string"},
                                                     "success": {"type": "boolean"}},
                    "required": ["summary"]}}},
]

SYSTEM = """You are Buddy's hands on the web. You control a real browser on the user's computer to get a job \
done for them, step by step. After each step you see the page as text with numbered things to click or type into.

The job and the user's answers come from speech-to-text, so words may be mis-heard: read them by sound and \
context ("club spark" is ClubSpark, "Lockhart" when logging in is "login").

Doing it yourself (most important):
- Go as far as you possibly can on your own before asking anything. For a sign-in: open the site, press "Sign in \
with Google" (or Microsoft) if it's there, pick the right account in Google's account chooser (this browser may \
already be signed in), fill in saved details, press next. Only ask_user when a step truly needs something you \
don't have, and then ask for just that one thing ("What's the password for your school email?").
- Never ask them for an exact name, address or ID you could work out, find on the page, or search for.

Finding the site:
- Names come from speech-to-text and are approximate: "Broncom", "Bromcom", "brom com VLE" are all Bromcom, whose \
student site is Bromcom VLE (https://www.bromcomvle.com/). Use common sense to find the real site.
- A login asking for a school ID or school name: work it out from the school email's domain (e.g. \
@christs.school is Christ's School) and their home area; search for the school's Bromcom ID if the page doesn't \
offer a list.
- The request comes from speech-to-text, so addresses arrive as words: "studdy buddy dot vercel dot app slash \
code" means studdy-buddy.vercel.app/code (try the obvious spellings, with and without dashes).
- They may only give a name ("ClubSpark", "the council tennis courts", "Pizza Express") or no site at all. Work out \
the site yourself: if you know it, go there (clubspark.lta.org.uk, not "clubspark" alone); otherwise search \
(https://duckduckgo.com/html/?q=... or https://www.google.com/search?q=...) and pick the official result. Add \
their area to the search when it's about somewhere local.
- Never give up because an address was vague or didn't load: try the other spelling, search for it, try another \
search engine, or a different page on the site.

How to work:
- Don't ask the user whether to do more or how much to read: decide sensibly yourself and get on with it. Only \
ask_user for something you truly can't do without (a missing password, a real choice like which time to book).
- Be quick: when you know the next few steps, do them in one go (e.g. type the email, type the password and click \
next as three tool calls in the same turn).
- Prefer the site's own buttons and forms. If something doesn't work, look again, scroll, or try another way.
- Be persistent: keep going until the job is really done. Only finish without success after several genuinely \
different attempts, and then say exactly what you tried and what stopped you.
- Choose sensible defaults from what the user said; ask_user only when a choice really matters to them \
(e.g. which of several times) or something is missing.
- Anything that commits them (booking, paying, ordering, sending, signing up, cancelling) needs ask_user with \
final=true first, saying exactly what will happen and the price. Only after a yes, click it.
- The browser is signed in to the user's Google account (if they've signed in to it once): use "Sign in with \
Google" where a site offers it, and Google services (Gmail, Calendar, Drive) are already signed in.
- If they have any saved account, NEVER ask them for a password out loud: always fill_login (it finds the \
right account from the page). Only ask_user when fill_login says nothing is saved.
- Google's sign-in pages: on the email page fill_login with the account and user_n; on the password page \
fill_login with password_n (it presses Enter for you). Don't type the password yourself.
- Logging in (the user is happy for you to handle their logins and passwords; it all stays on their own Mac):
  1. A login saved for this site: fill_login.
  2. Otherwise, their saved accounts: {accounts}. If it's obvious which (a school site: the school one), use it \
with fill_login(account=...); if not, ask_user which one ("your school email or your home email?").
  3. Nothing saved: ask_user for the email and password (they can say them out loud), type them in with \
type_text, and once you're signed in, call save_login (with account= if they said which one it is, e.g. "school \
email"), so next time it's automatic. Never read a password back out loud.
- Card or bank details: don't type them; ask them to fill those in themselves in the browser window.
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
        # Two browsers, each with its own sign-ins: school (the school Google account, Bromcom) and home
        # (your personal Google account: tennis, shopping, everything else).
        visible = bool(settings.get("online_show_browser", True))
        if browser is not None:
            self.browsers = {"school": browser, "home": browser}
        else:
            self.browsers = {"school": Browser(Path(settings.data_dir) / "browser", visible=visible),
                             "home": Browser(Path(settings.data_dir) / "browser-home", visible=visible)}
        self.browser = self.browsers["home"]
        self.job: Job | None = None
        self.say = None  # async (text, wait_for_answer: bool): the hub speaks it out loud
        self.logins = None  # LoginStore: saved website logins (passwords never go to Claude)
        self.report = None  # async (job): the hub offers the result when you're free
        self.on_busy = None  # (bool): a job started / ended (the board shows a slow pink light)
        self.history: list[Job] = []

    @property
    def busy(self) -> bool:
        return self.job is not None and self.job.status in ("working", "waiting")

    @property
    def waiting(self) -> bool:
        return self.job is not None and self.job.status == "waiting"

    def start(self, task: str, who: str = "", today: str = "", home: str = "", kind: str = "",
              quiet: bool = False) -> str:
        if not self.settings.get("online_on", True):
            return "Going online to do things is switched off in Settings (online_on)."
        if self.busy and self.job.quiet:
            self.stop()  # your request comes first; the early-morning job runs again later
        if self.busy:
            return f"Already busy with: {self.job.task}. Say stop first if you want something else."
        self.job = Job(task=task, who=who, kind=kind, quiet=quiet)
        self.browser = self.browsers["school" if kind.startswith("school") else "home"]
        self.job.task_handle = asyncio.create_task(self._run(self.job, today, home))
        if self.on_busy and not quiet:
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
        if self.job is not None and self.job.quiet:
            return
        if self.say:
            await self.say(text, listen)

    async def _ask(self, job: Job, question: str) -> str:
        if job.quiet:
            return ""  # nobody's up at 5am: carry on without, or finish and say what was missing
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
            if self.on_busy and not job.quiet:
                self.on_busy(False)

    async def _loop(self, job: Job, today: str, home: str) -> None:
        accts = self.logins.account_list() if self.logins else []
        known = ", ".join(f"{a['label']} ({a['email']}, " + ("ONLY for school sites" if self.logins.is_school(a['label'], a)
                          else "for everything that isn't school") + ")" for a in accts) or "none yet"
        if not job.kind.startswith("school"):
            known += (". This job isn't school, so never use the school email: use their home account (or Sign in with "
                      "Google choosing their personal account). If no home account is saved, ask_user for their home "
                      "email and password and save_login with account=\"home email\".")
        system = SYSTEM.format(today=today or time.strftime("%A %d %B %Y"), home=home or "unknown",
                               who=job.who or "the user", accounts=known)
        start = f"The job: {job.task}"
        shortcut = self.shortcuts().get(job.kind) if job.kind else None
        if shortcut:
            start += (f"\n\nLast time a job like this ended up at {shortcut}: open that first (you're probably "
                      "still signed in), and only go the long way round if it doesn't work.")
        messages: list[dict] = [{"role": "system", "content": system}, {"role": "user", "content": start}]
        # The quick model for each step (much faster); the bigger one takes over if it keeps getting stuck.
        tier, stuck = "fast", 0
        for _ in range(MAX_STEPS):
            if job.status == "stopped":
                return
            res = await self.llm.chat(messages, tier=tier, tools=TOOLS, max_tokens=1200)
            if not res.tool_calls:
                messages.append({"role": "assistant", "content": res.content or "..."})
                messages.append({"role": "user", "content": "Use the tools: carry on with the job, or call finish."})
                continue
            messages.append(res.assistant_message)
            for call in res.tool_calls:
                name, a = call["function"]["name"], _args(call)
                result = await self._tool(job, name, a)
                if job.status in ("done", "stopped"):
                    if job.status == "done" and job.kind and self.browser.page is not None:
                        self.save_shortcut(job.kind, self.browser.page.url)
                    return
                if result.startswith(("That didn't work", "No such number", "Nothing saved", "unknown tool")):
                    stuck += 1
                    if stuck >= 3 and tier == "fast":
                        tier = "smart"
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
            self._trim(messages)
        job.status, job.result = "failed", "too many steps"
        await self._speak(f"I couldn't finish {job.task}: it needed too many steps. Nothing was booked or paid.")

    def shortcuts(self) -> dict:
        try:
            return json.loads(self._shortcut_path().read_text())
        except (OSError, json.JSONDecodeError, TypeError, AttributeError):
            return {}

    def save_shortcut(self, kind: str, url: str) -> None:
        if not url.startswith("http"):
            return
        data = self.shortcuts()
        data[kind] = url
        try:
            self._shortcut_path().write_text(json.dumps(data, indent=2))
        except (OSError, TypeError, AttributeError):
            pass

    def _shortcut_path(self) -> Path:
        return Path(self.settings.data_dir) / "online_shortcuts.json"

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
        result = await self._tool_inner(job, name, a)
        if self.browser.page is not None and result.startswith("Page:"):
            steps = await self._autopilot(job)
            if steps:
                result = f"(Signed in by myself: {', '.join(steps)}.)\n" + await self.browser.look()
        return result

    async def _autopilot(self, job: Job) -> list[str]:
        """Google's sign-in steps, done straight away in code (no thinking needed): pick the saved account,
        type its email or password, press Continue / Allow. Much quicker than a step at a time."""
        b, done = self.browser, []
        if not self.logins:
            return done
        accts = self.logins.accounts()
        for _ in range(6):
            page = b.page
            if "accounts.google.com" not in (page.url or ""):
                break
            try:
                text = (await page.inner_text("body", timeout=2000)).lower()
            except Exception:  # noqa: BLE001
                break
            school = job.kind.startswith("school")
            acct = self.logins.account_on_page(text)
            if acct is not None and self.logins.is_school(*acct) != school:
                acct = None  # the school email only for school things; the home one for everything else
            if acct is None:
                acct = self.logins.pick(school)
            if acct is None:
                break  # no account of the right kind saved: the step-by-step way asks for it
            moved = False
            try:
                pw = page.locator('input[type="password"]:visible').first
                email = page.locator('input[type="email"]:visible').first
                if acct and await pw.count() and acct[1].get("password"):
                    await pw.fill(acct[1]["password"], timeout=4000)
                    await pw.press("Enter")
                    done.append("password")
                    moved = True
                elif acct and await email.count():
                    await email.fill(acct[1].get("email", ""), timeout=4000)
                    await email.press("Enter")
                    done.append("email")
                    moved = True
                elif (acct and acct[1].get("email") and "choose an account" in text
                      and await page.get_by_text(acct[1]["email"], exact=False).filter(visible=True).count()):
                    await page.get_by_text(acct[1]["email"], exact=False).filter(visible=True).first.click(timeout=4000)
                    done.append("picked the account")
                    moved = True
                else:
                    btn = page.get_by_role("button", name=re.compile(r"^(continue|continue as .*|allow|next)$", re.I)
                                           ).filter(visible=True).first
                    if await btn.count():
                        await btn.click(timeout=4000)
                        done.append("continue")
                        moved = True
            except Exception:  # noqa: BLE001 - leave it to the step-by-step way
                break
            if not moved:
                break
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.8)  # Google's pages animate between steps
        if done:
            job.steps.append("signed in: " + ", ".join(done))
        return done

    async def _tool_inner(self, job: Job, name: str, a: dict) -> str:
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
                is_pw = "password" in it["kind"] or "password" in it["meta"].lower()
                if CARD.search(f"{it['label']} {it['meta']}"):
                    return ("Never type card or bank details. Use ask_user to ask them to fill those in themselves "
                            "in the browser window, then look again.")
                text = str(a.get("text", ""))
                if is_pw:
                    job.typed_password = text  # kept for save_login; never shown in the steps or logs
                    job.steps.append("typed the password")
                else:
                    if re.search(r"email|user|login|identifier|account", f"{it['kind']} {it['meta']} {it['label']}", re.I):
                        job.typed_user = text
                    job.steps.append(f"typed into {it['label'][:30]}")
                await b.type(it["n"], text, bool(a.get("enter")))
                return await b.look()
            if name == "save_login":
                if not self.logins or not job.typed_password:
                    return "Nothing to save: no password was typed in this job."
                try:
                    site = self.logins.save(b.page.url, job.typed_user, job.typed_password)
                    note = f"Saved the login for {site}"
                except ValueError:  # a page with no web address (e.g. a file)
                    note = "Saved"
                if a.get("account"):
                    label = self.logins.save_account(a["account"], job.typed_user, job.typed_password)
                    note += f' and as their "{label}"'
                job.steps.append("saved the login")
                return note + "."
            if name == "fill_login":
                if not self.logins:
                    return "No saved logins."
                if a.get("account"):
                    found = self.logins.account(a["account"])
                    if found and self.logins.is_school(*found) and not job.kind.startswith("school"):
                        home = self.logins.pick(False)
                        if not home:
                            return ("That's their school email: only for school things. Ask_user for their home email "
                                    "and password, then save_login with account=\"home email\".")
                        found = home
                    login = {"username": found[1].get("email", ""), "password": found[1].get("password", "")} if found else None
                    where = f'their "{found[0]}"' if found else ""
                else:
                    f = self.logins.find(b.page.url)
                    login, where = (f[1], f"the saved login for {f[0]}") if f else (None, "")
                    if not login:  # e.g. Google's password page: the account it shows on the page
                        try:
                            shown = self.logins.account_on_page(await b.page.inner_text("body", timeout=3000))
                        except Exception:  # noqa: BLE001
                            shown = None
                        if shown:
                            login = {"username": shown[1].get("email", ""), "password": shown[1].get("password", "")}
                            where = f'their "{shown[0]}"'
                if not login:
                    return ("Nothing saved for that. Check their saved accounts, or ask_user for the email and "
                            "password, type them in, and save_login once signed in.")
                done = []
                if a.get("user_n"):
                    if not b.item(a["user_n"]):
                        return "user_n isn't on the page: look again."
                    await b.type(a["user_n"], login.get("username", ""))
                    job.typed_user = login.get("username", "")
                    done.append(f"username {login.get('username', '')}")
                if a.get("password_n"):
                    pw = b.item(a["password_n"])
                    if not pw or "password" not in f"{pw['kind']} {pw['meta']}".lower():
                        return "password_n isn't a password box: look again."
                    if not login.get("password"):
                        return "There's no password saved for that: ask_user for it."
                    await b.type(pw["n"], login["password"], enter=True)  # and press Enter: Next on most sign-ins
                    job.typed_password = login["password"]
                    done.append("the password")
                if not done:
                    return "Give user_n and/or password_n."
                job.steps.append(f"filled in {where}")
                return f"Filled in {' and '.join(done)} from {where}. Now click next or log in."
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
                job.topic = str(a.get("topic", "")).strip()[:40]
                if self.report:
                    await self.report(job)  # told when the conversation's over, not over the top of it
                else:
                    await self._speak(job.result)
                return "ok"
        except Exception as e:  # noqa: BLE001 - a page error goes back to Claude to try another way
            return f"That didn't work: {str(e).splitlines()[0][:200]}. Look again and try another way."
        return f"unknown tool {name}"


YES = re.compile(r"\b(yes|yeah|yep|yup|sure|go ahead|do it|please do|ok|okay|book it|confirm|correct|that's right|"
                 r"sounds good|perfect|done|go for it)\b", re.I)
NO = re.compile(r"\b(no|nope|don't|do not|stop|wait|hold on|not yet|cancel|never mind)\b", re.I)
