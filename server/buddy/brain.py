"""The conversation brain: fast-path intents first, then Claude Haiku with tools.

- Common commands never touch the model (intents.py), so they're instant and free.
- Action tools (alarms, memory, volume) are confirmed with our own sentence instead of a
  second model call; only questions go through a follow-up call, with a short history.
- Haiku answers quickly by default. For long writing, planning or tricky reasoning it can
  think much harder, but only after asking ("Want me to have a proper think?"), unless
  the user already asked it to think hard.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from . import intents
from .alarms import DAY_NAMES, AlarmStore, human_duration, ordinal, spoken_time
from .config import Settings
from .llm import LLMError
from .memory import MemoryManager, MemoryStore
from .spotify import SpotifyError as SpotifySetupError
from .timeparse import find_clock, find_date, find_repeat, resolve_clock, words_to_digits
from .weather import WMO, Weather

log = logging.getLogger("buddy.brain")

# Asking for it counts as permission: no need to ask "shall I think hard?" first.
THINK_REQUEST = re.compile(
    r"\b(think (harder|hard|properly|carefully|it through|about (it|this|that) properly)|"
    r"have a (proper |good |real )?think|take your time|use your big brain|really think)\b", re.I)
YES = re.compile(r"^\W*(yes|yeah|yep|yup|sure|please|go on|go for it|do it|ok(ay)?|alright|why not|"
                 r"go ahead|definitely|of course|think (about it|hard)|yes please)\b", re.I)
NO = re.compile(r"^\W*(no|nah|nope|don'?t|quick(ly)?|just (tell|give|answer)|short (answer|version)|"
                r"not now|no thanks?)\b", re.I)
# Said out loud while Buddy goes off to do something slow, so it never just goes quiet.
WHILE_I = {
    "search": ["One sec, I'll look that up.", "Give me a sec, I'll check.", "Hang on, let me have a look.",
               "Two secs, I'll find out.", "Let me check that for you."],
    "think": ["Right, let me have a proper think.", "Okay, give me a moment to think this through.",
              "Ooh, good one. Let me have a think.", "Leave it with me a sec."],
}


async def say_while(ctx: dict, kind: str) -> None:
    """Speak a short "one sec" line once per turn, if the caller can (ctx["say"])."""
    say = ctx.get("say")
    if say and not ctx.get("said_while"):
        ctx["said_while"] = True
        await say(random.choice(WHILE_I[kind]))


# Ready-made replies that get reworded by Claude so they don't always sound the same. Not:
# Claude's own answers (already natural), "say that again" (must repeat exactly), long lists, silence.
REWORD = {"alarm", "alarm_in", "alarm_list", "alarm_cancel_all", "alarm_needs_time", "timer", "reminder", "snooze",
          "volume", "weather", "thanks", "help", "time", "date", "voice", "music", "llm_tools", "llm_error"}

THINK_OFFER = "That's a meaty one. It'll take a few more seconds, but want me to have a proper think about it?"


@dataclass
class Reply:
    text: str
    actions: list[dict] = field(default_factory=list)
    follow_up: bool = False
    model: str = ""
    intent: str = ""
    ms: int = 0


@dataclass
class Conversation:
    history: list[dict] = field(default_factory=list)  # user/assistant only, for context
    transcript: list[dict] = field(default_factory=list)  # whole session, for memory learning
    last_active: float = field(default_factory=time.time)
    last_reply: str = ""
    pager: int | None = None
    pending: dict | None = None
    clear_requested: float = 0.0

    def add(self, role: str, content: str, note: str = "") -> None:
        """note: private background for later turns (e.g. "I searched the web for ..."), never spoken."""
        if not content:
            return
        self.history.append({"role": role, "content": content, **({"note": note} if note else {})})
        self.history = self.history[-8:]
        self.transcript.append({"role": role, "content": content})
        self.last_active = time.time()


TOOLS = [
    {"type": "function", "function": {
        "name": "set_alarm", "description": "Set an alarm (rings until stopped).",
        "parameters": {"type": "object", "properties": {
            "time": {"type": "string", "description": "24h local time HH:MM"},
            "repeat": {"type": "string", "description": "none, daily, weekdays, weekends, or days like 'mon,wed'"},
            "date": {"type": "string", "description": "YYYY-MM-DD, only for a one-off on a specific day"},
            "label": {"type": "string"}}, "required": ["time"]}}},
    {"type": "function", "function": {
        "name": "set_timer", "description": "Start a countdown timer.",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "integer"}, "label": {"type": "string"}}, "required": ["seconds"]}}},
    {"type": "function", "function": {
        "name": "set_reminder", "description": "Remind the user of something, at a time or after a delay.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "what to remind them, e.g. 'take the bins out'"},
            "time": {"type": "string", "description": "24h local HH:MM"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
            "repeat": {"type": "string"},
            "in_seconds": {"type": "integer"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "list_alarms", "description": "List alarms, timers and reminders with their ids.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "cancel_alarm", "description": "Cancel alarms/timers/reminders by id (see list_alarms), or all of a kind.",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}},
            "all_of_kind": {"type": "string", "description": "alarm, timer, reminder or all"}}}}},
    {"type": "function", "function": {
        "name": "get_weather", "description": "Forecast for the user's home: today, tomorrow or the week ahead.",
        "parameters": {"type": "object", "properties": {
            "day": {"type": "string", "enum": ["today", "tomorrow", "week"]}}}}},
    {"type": "function", "function": {
        "name": "remember", "description": "Save a lasting fact or preference about the user.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string", "description": "short third-person statement"},
            "category": {"type": "string", "enum": ["about_you", "people", "preferences", "routines", "plans", "other"]}},
            "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "forget", "description": "Delete memories by id.",
        "parameters": {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "integer"}}}, "required": ["ids"]}}},
    {"type": "function", "function": {
        "name": "update_memory", "description": "Rewrite one memory.",
        "parameters": {"type": "object", "properties": {
            "id": {"type": "integer"}, "text": {"type": "string"}}, "required": ["id", "text"]}}},
    {"type": "function", "function": {
        "name": "clear_memory", "description": "Erase all memories. Only after the user explicitly confirmed.",
        "parameters": {"type": "object", "properties": {"confirmed": {"type": "boolean"}}, "required": ["confirmed"]}}},
    {"type": "function", "function": {
        "name": "set_volume", "description": "Speaker volume 0-10, absolute or relative.",
        "parameters": {"type": "object", "properties": {
            "level": {"type": "integer"}, "change": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "music", "description": "Control Spotify. action: play (query = song, artist, album or playlist "
                                        "to search for; for moods use a playlist-style query like 'upbeat bangers'), "
                                        "pause, resume, next, previous, volume (volume_percent 0-100), now_playing.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["play", "pause", "resume", "next", "previous", "volume", "now_playing"]},
            "query": {"type": "string"}, "kind": {"type": "string", "enum": ["track", "artist", "album", "playlist"]},
            "volume_percent": {"type": "integer"}}, "required": ["action"]}}},
    {"type": "function", "function": {
        "name": "switch_voice", "description": "Change the voice you speak with, among the user's saved voices. "
                                               "step 1 = next, -1 = previous; or a 1-based number; or a voice name.",
        "parameters": {"type": "object", "properties": {
            "step": {"type": "integer"}, "number": {"type": "integer"}, "name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "web_search", "description": "Search the web for current or factual info (news, sport, prices, opening times, events).",
        "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "think_harder", "description": "Think much harder about this request (long writing, planning, tricky "
                                                "reasoning). Buddy asks the user's permission first.",
        "parameters": {"type": "object", "properties": {"reason": {"type": "string"}}}}},
]
ACTION_TOOLS = {"set_alarm", "set_timer", "set_reminder", "cancel_alarm", "remember", "forget",
                "update_memory", "clear_memory", "set_volume", "switch_voice", "music"}

SYSTEM = """You are {name}, {user_ref} voice assistant. You live in {user_ref} bedroom and you've known \
{user_obj} for a while: you're a friend, not a gadget or a servant. Everything you say is spoken aloud.
How to be:
- Talk like a good mate who knows {user_obj}: warm, relaxed, easy-going British English, a bit of dry humour \
when it fits. No formal assistant phrases ("How may I assist you", "Certainly!", "Great question").
- Getting things done (alarms, timers, reminders, volume, weather, quick facts): be quick and to the point. \
Do it and confirm in a few words, e.g. "Done, alarm's set for 7." No chat, no follow-up offers.
- Just chatting: be friendly and natural, like a friend would. Ask something back now and then, not every time.
- Usually one or two sentences; go longer only when asked.
- Never describe yourself as a device, a speaker, a desk companion or a desk assistant, and don't talk about \
where you are unless asked.
No lists, markdown, emojis or links. Say times like "7:30 AM" and temperatures like "12 degrees".
Now: {now}. Home: {place}.
{weather}
What you remember about the user (numbers are ids for editing):
{memories}
Rules:
- Use tools for alarms, timers, reminders, weather, memory, volume, music (Spotify) and changing your voice. Times are 24h HH:MM local.
- If a time or detail is missing or ambiguous, ask one short question.
- "Remember ..." means use remember. To forget or change a memory, use forget or update_memory with its id.
- Before clear_memory, ask the user to confirm; only call it with confirmed=true after they say yes.
- For news, sport, prices, opening hours or anything recent, use web_search. (You've already said "one sec, \
I'll check" out loud before searching, so don't start your answer with that.)
- For long writing, planning or tricky reasoning, call think_harder (it asks the user first).
- If you ask the user something, end with a question mark.
- Never say you did something unless a tool confirmed it.
- Trust your earlier answers in this conversation: they were right. "(Background note ...)" lines are your own \
private notes about earlier turns (for example a web search you did); use them, never read them out, and don't \
apologise for or doubt earlier answers unless the user says they were wrong.
- If something goes wrong, be relaxed and kind about it, say what you can do instead, and keep it short."""


def speakable(text: str, limit: int = 900) -> str:
    t = re.sub(r"https?://\S+", "", text or "")
    t = re.sub(r"```.*?```", "", t, flags=re.S)
    t = re.sub(r"\*\*|__|`|#+\s*|^\s*[-*•]\s+", "", t, flags=re.M)
    t = re.sub(r"\[(.*?)\]\(.*?\)", r"\1", t)
    t = re.sub(r"[\U0001F300-\U0001FAFF☀-➿]", "", t)
    t = re.sub(r"【[^】]*】", "", t)  # citation markers from web search
    t = re.sub(r"\s*\n+\s*", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    if len(t) > limit:
        cut = t[:limit]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[:end + 1] if end > limit * 0.5 else cut) + " Want me to carry on?"
    return t


class Brain:
    def __init__(self, settings: Settings, llm, alarms: AlarmStore, memory: MemoryStore,
                 memory_mgr: MemoryManager, weather: Weather):
        self.settings = settings
        self.llm = llm
        self.alarms = alarms
        self.memory = memory
        self.memory_mgr = memory_mgr
        self.weather = weather
        self.volume = int(settings["volume"])

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings["timezone"]))

    async def handle(self, text: str, conv: Conversation, ctx: dict | None = None) -> Reply:
        ctx = ctx or {}
        t0 = time.monotonic()
        text = intents.strip_wake(text)
        if not text:
            return Reply("", intent="empty")
        reply = await self._pending(text, conv, ctx)
        if reply is None:
            intent = intents.match(text, self.now(), {"ringing": ctx.get("ringing"), "pager": conv.pager is not None})
            reply = await self._intent(intent, conv, ctx) if intent else None
        if reply is None:
            reply = await self._llm(text, conv, ctx)
        reply.text = speakable(reply.text, 1400 if reply.intent == "llm_deep" else 900)
        if reply.text and reply.intent in REWORD and self.settings["reword_replies"]:
            reply.text = await self._reword(text, reply.text)
        conv.add("user", text)
        if reply.text:
            notes = [f'I searched the web for "{q}" and found: {r[:350]}' for q, r in ctx.get("searched", [])]
            if reply.intent == "llm_deep":
                notes.append("I thought this one through carefully.")
            conv.add("assistant", reply.text, " ".join(notes))
            conv.last_reply = reply.text
        if reply.intent != "more_memory" and reply.intent != "memory_list":
            conv.pager = None
        mode = self.settings["follow_up"]
        if mode == "always" and reply.text:
            reply.follow_up = True
        elif mode == "questions" and reply.text.rstrip().endswith("?"):
            reply.follow_up = True
        elif mode == "off":
            reply.follow_up = False
        if reply.intent == "off":
            reply.follow_up = False  # it's going to sleep: don't keep listening
        reply.ms = int((time.monotonic() - t0) * 1000)
        return reply

    # ---------- things we were waiting for ----------
    async def _pending(self, text: str, conv: Conversation, ctx: dict) -> Reply | None:
        p, conv.pending = conv.pending, None
        if not p:
            return None
        if p.get("intent") == "think":
            if YES.search(text):
                await say_while(ctx, "think")
                return await self._llm(p["text"], conv, ctx, tier="smart")
            if NO.search(text):
                return await self._llm(p["text"], conv, ctx, allow_think=False)
            return None  # something else entirely: treat it as a new request
        if p.get("intent") == "alarm_time":
            digits = words_to_digits(text)
            clock = find_clock(digits)
            if clock:
                now = self.now()
                hhmm = resolve_clock(clock, now, prefer_morning=True)
                repeat = find_repeat(digits)
                a = self.alarms.add_alarm(hhmm, repeat or None, "", None if repeat else find_date(digits, now))
                return Reply(self.alarms.confirm_text(a), intent="alarm")
        return None

    # ---------- deterministic ----------
    async def _intent(self, it: intents.Intent, conv: Conversation, ctx: dict) -> Reply | None:
        a = it.args
        now = self.now()
        n = it.name
        if n == "off":
            acts = [{"type": "stop"}, {"type": "sleep"}]  # quietly: no words, just off
            if ctx.get("ringing"):
                acts.append({"type": "alarm_stop"})
            return Reply("", acts, intent=n)
        if n == "stop":
            acts = [{"type": "stop"}]
            if ctx.get("ringing"):
                acts.append({"type": "alarm_stop"})
            return Reply("", acts, intent=n)
        if n == "snooze":
            minutes = a.get("minutes") or int(self.settings["snooze_minutes"])
            last = self.alarms.get(ctx["ringing"]) if ctx.get("ringing") else self.alarms.last_fired()
            if not ctx.get("ringing") and not last:
                return Reply("There's nothing ringing to snooze.", intent=n)
            self.alarms.snooze(last, minutes)
            return Reply(f"Okay, snoozing for {minutes} minutes.", [{"type": "alarm_stop", "quiet": True}], intent=n)
        if n == "thanks":
            return Reply(random.choice(["You're welcome.", "Any time.", "No problem.", "My pleasure."]), intent=n)
        if n == "repeat":
            return Reply(conv.last_reply or "I haven't said anything yet.", intent=n)
        if n == "more":
            if conv.pager is None:
                return None
            text, nxt = self.memory.spoken_list(conv.pager)
            conv.pager = nxt
            return Reply(text, intent="more_memory")
        if n == "voice":
            return self._switch_voice(a)
        if n == "music":
            return Reply(await self._music(a), intent=n)
        if n == "volume":
            level = a["level"] if "level" in a else self.volume + a.get("delta", 0)
            self.volume = max(0, min(10, level))
            self.settings.update({"volume": self.volume})
            return Reply(f"Volume {self.volume}." if self.volume else "",
                         [{"type": "volume", "level": self.volume}], intent=n)
        if n == "time":
            return Reply(f"It's {spoken_time(now)}.", intent=n)
        if n == "date":
            return Reply(f"It's {DAY_NAMES[now.weekday()]} the {ordinal(now.day)} of {now.strftime('%B')}.", intent=n)
        if n == "help":
            return Reply("I can set alarms, timers and reminders, tell you the weather, answer questions, "
                         "search the web and remember things about you. Just say hey buddy and ask.", intent=n)
        if n == "memory_list":
            text, nxt = self.memory.spoken_list(0)
            conv.pager = nxt
            return Reply(text, intent=n, follow_up=nxt is not None)
        if n == "alarm_list":
            return Reply(self._list_alarms_spoken(a.get("kind")), intent=n)
        if n == "alarm_cancel_all":
            count = self.alarms.cancel_all(a.get("kind"))
            what = a.get("kind", "alarm")
            if not count:
                return Reply(f"You don't have any {what}s set.", intent=n)
            return Reply(f"Done, I've cancelled {count} {what}{'s' if count != 1 else ''}.", intent=n)
        if n == "weather":
            return Reply(await self.weather.spoken(a.get("day", "today")), intent=n)
        if n == "timer":
            al = self.alarms.add_timer(a["seconds"], a.get("label", ""))
            return Reply(self.alarms.confirm_text(al), intent=n)
        if n == "alarm_in":
            self.alarms.add_timer(a["seconds"], "", kind="alarm")
            return Reply(f"Okay, I'll wake you in {human_duration(a['seconds'])}.", intent=n)
        if n == "alarm":
            al = self.alarms.add_alarm(a["time"], a.get("repeat") or None, a.get("label", ""), a.get("date"))
            return Reply(self.alarms.confirm_text(al), intent=n)
        if n == "alarm_needs_time":
            conv.pending = {"intent": "alarm_time"}
            return Reply("What time should I set it for?", intent=n, follow_up=True)
        if n == "reminder":
            if "seconds" in a:
                al = self.alarms.add_reminder_in(a["seconds"], a["text"])
                return Reply(f"Okay, I'll remind you to {a['text']} in {human_duration(a['seconds'])}.", intent=n)
            al = self.alarms.add_alarm(a["time"], a.get("repeat") or None, a["text"], a.get("date"), kind="reminder")
            return Reply(self.alarms.confirm_text(al), intent=n)
        return None

    async def _reword(self, said_by_user: str, reply: str) -> str:
        """Say a quick, ready-made reply in Buddy's own words so it doesn't sound the same every time.
        Facts stay exact. Falls back to the original if Claude is slow or unavailable."""
        user = self.settings["user_name"] or "the user"
        system = (f"You are {self.settings['assistant_name']}, {user}'s voice assistant and friend, in their bedroom. "
                  "Reword the reply below so it sounds natural, warm and a bit different each time, like a mate "
                  "talking. Keep every fact exactly: times, dates, numbers, names, temperatures. Keep it about as "
                  "short as the original (for quick tasks, a few words). If it's about a problem, be kind and "
                  "relaxed about it and keep any instruction in it. British English. Spoken aloud: no lists, "
                  "markdown or emojis. Reply with only the words to say.")
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f'They said: "{said_by_user}"\nReply to reword: "{reply}"'}]
        try:
            res = await asyncio.wait_for(self.llm.chat(msgs, tier="fast", max_tokens=200), timeout=4.0)
        except (asyncio.TimeoutError, LLMError, Exception) as e:  # noqa: BLE001 - never lose the reply
            log.info("rewording skipped: %s", e)
            return reply
        out = (res.content or "").strip().strip('"').strip()
        # Never let a reword drop a number (a time, a temperature) that was in the original.
        if not out or any(n not in out for n in re.findall(r"\d+", reply)):
            return reply
        return out

    async def _music(self, a: dict) -> str:
        """Spotify. Returns what to say ("" for pause and skip: the music says it all)."""
        sp = getattr(self, "spotify", None)
        if sp is None or not sp.connected:
            return "Spotify isn't connected yet. You can set it up in Controls, under Settings."
        try:
            act = a.get("action", "play")
            if act == "play":
                return await sp.play(a.get("query", ""), a.get("kind", ""))
            if act == "resume":
                await sp.play()
                return ""
            if act == "pause":
                return await sp.pause()
            if act in ("next", "previous"):
                return await sp.skip(back=act == "previous")
            if act == "volume":
                return await sp.set_volume(a.get("volume_percent", 50))
            if act == "now_playing":
                return await sp.now_playing()
            return "I'm not sure how to do that with Spotify."
        except SpotifySetupError as e:
            return str(e)

    def _switch_voice(self, a: dict) -> Reply:
        voices = [v for v in (self.settings["voices"] or []) if isinstance(v, dict) and (v.get("id") or "").strip()]
        if len(voices) < 2 and not a.get("name"):
            return Reply("You've only got one voice set up. Add more in Controls, under Voices.", intent="voice")
        current = next((i for i, v in enumerate(voices) if v["id"].strip() == self.settings["elevenlabs_voice_id"]), -1)
        if "number" in a:
            if not 1 <= a["number"] <= len(voices):
                return Reply(f"I've only got {len(voices)} voices.", intent="voice")
            i = a["number"] - 1
        elif "name" in a:
            want = a["name"].lower().strip()
            i = next((j for j, v in enumerate(voices) if (v.get("name") or "").lower().strip() == want), None)
            if i is None:
                i = next((j for j, v in enumerate(voices) if want in (v.get("name") or "").lower()), None)
            if i is None:
                return Reply(f"I haven't got a voice called {a['name']}.", intent="voice")
        else:
            i = (current + a.get("step", 1)) % len(voices)
        v = voices[i]
        self.settings.update({"elevenlabs_voice_id": v["id"].strip()})
        name = (v.get("name") or "").strip()
        return Reply(f"How's this? This is {name}." if name else f"How's this? This is voice {i + 1}.", intent="voice")

    def _list_alarms_spoken(self, kind: str | None = None) -> str:
        now = time.time()
        items = [x for _, x in self.alarms.upcoming(now, horizon_s=400 * 86400, limit=100)]
        seen, uniq = set(), []
        for x in items:
            if x.id not in seen:
                seen.add(x.id)
                uniq.append(x)
        if kind == "timer":
            uniq = [x for x in uniq if x.kind == "timer"]
            if not uniq:
                return "You don't have any timers running."
        if not uniq:
            return "You don't have any alarms, timers or reminders set."
        descs = [self.alarms.describe(x, now) for x in uniq[:6]]
        head = f"You have {len(uniq)}: " if len(uniq) > 1 else "You have "
        body = ", ".join(descs[:-1]) + (", and " if len(descs) > 1 else "") + descs[-1]
        more = f" Plus {len(uniq) - 6} more." if len(uniq) > 6 else ""
        return head + body + "." + more

    # ---------- the model ----------
    async def system_prompt(self) -> str:
        now = self.now()
        user = self.settings["user_name"]
        return SYSTEM.format(
            name=self.settings["assistant_name"],
            user_ref=f"{user}'s" if user else "the user's", user_obj=user or "them",
            now=f"{DAY_NAMES[now.weekday()]} {now.day} {now.strftime('%B %Y')}, {now.strftime('%H:%M')} "
                f"({self.settings['timezone']})",
            place=self.settings["location_name"],
            weather=await self.weather.context_line(),
            memories=self.memory.render_for_prompt(1800),
        )

    async def _llm(self, text: str, conv: Conversation, ctx: dict, tier: str = "fast",
                   allow_think: bool = True) -> Reply:
        if THINK_REQUEST.search(text):
            tier = "smart"
        if tier == "smart":
            await say_while(ctx, "think")
        messages = [{"role": "system", "content": await self.system_prompt()}]
        for m in conv.history[-6:]:
            content = m["content"]
            if m.get("note"):
                content = f"(Background note, not spoken: {m['note']})\n{content}"
            messages.append({"role": m["role"], "content": content})
        messages.append({"role": "user", "content": text})
        tools = TOOLS if allow_think else [t for t in TOOLS if t["function"]["name"] != "think_harder"]
        try:
            return await self._tool_loop(messages, conv, ctx, tier, tools, text)
        except LLMError as e:
            log.warning("LLM failed: %s", e)
            err = str(e)
            if "No Claude API key" in err:
                return Reply("I need a Claude API key before I can answer that. You can add one in the web page "
                             "settings.", intent="llm_error")
            if "no credit" in err:
                return Reply("My Claude account has run out of credit. You can top it up at console dot "
                             "anthropic dot com. Alarms, timers and the weather still work.", intent="llm_error")
            if "didn't accept" in err:
                return Reply("Claude didn't accept my key. Have a look at the key in the web page settings.",
                             intent="llm_error")
            return Reply("Ah, my brain's having a little wobble right now. Give it a minute and ask me again. "
                         "Alarms, timers and the weather still work fine though.", intent="llm_error")

    async def _tool_loop(self, messages: list[dict], conv: Conversation, ctx: dict, tier: str,
                         tools: list[dict], request: str) -> Reply:
        actions: list[dict] = []
        confirmations: list[str] = []
        model = ""
        intent = "llm_deep" if tier == "smart" else "llm"
        for _ in range(4):
            res = await self.llm.chat(messages, tier=tier, tools=tools, max_tokens=900 if tier == "smart" else 500)
            model = res.model
            if not res.tool_calls:
                text = res.content or " ".join(confirmations)
                return Reply(text, actions, model=model, intent=intent)
            names = [c["function"]["name"] for c in res.tool_calls]
            if "think_harder" in names and tier != "smart":
                # Thinking hard is slower and costs more, so ask first. A yes reruns it (see _pending).
                conv.pending = {"intent": "think", "text": request}
                return Reply(THINK_OFFER, actions, model=model, intent="think_offer", follow_up=True)
            messages.append(res.assistant_message)
            info_needed = False
            for call in res.tool_calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result, confirm, acts = await self._run_tool(name, args, conv, ctx)
                actions += acts
                if confirm:
                    confirmations.append(confirm)
                if name not in ACTION_TOOLS:
                    info_needed = True
                messages.append({"role": "tool", "tool_call_id": call.get("id", name), "content": result})
            if not info_needed and confirmations:
                # Every call was an action with its own confirmation: skip a second model round trip.
                return Reply(" ".join(confirmations), actions, model=model, intent="llm_tools")
        return Reply(" ".join(confirmations) or "Oops, I got myself a bit tangled up there. Mind asking me that again?", actions,
                     model=model, intent="llm_tools")

    async def _run_tool(self, name: str, a: dict, conv: Conversation, ctx: dict) -> tuple[str, str, list]:
        """Returns (result for the model, spoken confirmation or '', device actions)."""
        try:
            if name == "set_alarm":
                al = self.alarms.add_alarm(a["time"], a.get("repeat") or None, a.get("label", ""), a.get("date") or None)
                c = self.alarms.confirm_text(al)
                return f"ok id={al.id}: {c}", c, []
            if name == "set_timer":
                al = self.alarms.add_timer(int(a["seconds"]), a.get("label", ""))
                c = self.alarms.confirm_text(al)
                return f"ok id={al.id}", c, []
            if name == "set_reminder":
                if a.get("in_seconds"):
                    al = self.alarms.add_reminder_in(int(a["in_seconds"]), a["text"])
                    c = f"Okay, I'll remind you to {a['text']} in {human_duration(int(a['in_seconds']))}."
                elif a.get("time"):
                    al = self.alarms.add_alarm(a["time"], a.get("repeat") or None, a["text"], a.get("date") or None,
                                               kind="reminder")
                    c = self.alarms.confirm_text(al)
                else:
                    return "error: need time or in_seconds; ask the user when", "", []
                return f"ok id={al.id}", c, []
            if name == "list_alarms":
                now = time.time()
                rows = [f"{x.id}: {self.alarms.describe(x, now)}" for x in self.alarms.list()]
                return "\n".join(rows) or "none", "", []
            if name == "cancel_alarm":
                if a.get("all_of_kind"):
                    k = a["all_of_kind"]
                    count = self.alarms.cancel_all(None if k == "all" else k)
                    return f"cancelled {count}", f"Done, cancelled {count}." if count else "There was nothing to cancel.", []
                gone = [self.alarms.cancel(i) for i in a.get("ids", [])]
                gone = [g for g in gone if g]
                if not gone:
                    return "error: no such id; call list_alarms", "", []
                c = "Cancelled " + " and ".join(self.alarms.describe(g) for g in gone) + "."
                return "cancelled", c, []
            if name == "get_weather":
                day = a.get("day", "today")
                if day == "week":
                    return await self._week_weather(), "", []
                return await self.weather.spoken(day), "", []
            if name == "remember":
                m, created = self.memory.add(a["text"], a.get("category", "other"), pinned=True, source="user")
                return f"saved as #{m.id}", "Got it, I'll remember that." if created else "I already knew that, I've updated it.", []
            if name == "forget":
                removed = self.memory.delete([int(i) for i in a.get("ids", [])])
                if not removed:
                    return "error: no such id", "", []
                what = removed[0].text.rstrip(".") if len(removed) == 1 else f"{len(removed)} things"
                return "deleted", f"Okay, I've forgotten that: {what}." if len(removed) == 1 else f"Okay, I've forgotten {what}.", []
            if name == "update_memory":
                m = self.memory.update(int(a["id"]), a["text"])
                return ("updated", f"Updated. It now says: {m.text}", []) if m else ("error: no such id", "", [])
            if name == "clear_memory":
                if a.get("confirmed") and time.time() - conv.clear_requested < 120:
                    n = self.memory.clear()
                    conv.clear_requested = 0
                    return "cleared", f"Done. I've wiped my memory, {n} things. There's a backup if you change your mind.", []
                conv.clear_requested = time.time()
                return "not cleared: ask the user to confirm they want everything erased", "", []
            if name == "set_volume":
                level = a.get("level")
                level = self.volume + int(a.get("change", 0)) if level is None else int(level)
                self.volume = max(0, min(10, level))
                self.settings.update({"volume": self.volume})
                return f"volume {self.volume}", f"Volume {self.volume}.", [{"type": "volume", "level": self.volume}]
            if name == "music":
                said = await self._music(a)
                return said or "done", said, []
            if name == "switch_voice":
                args = {k: a[k] for k in ("number", "name") if a.get(k)} or {"step": int(a.get("step") or 1)}
                r = self._switch_voice(args)
                return r.text, r.text, []
            if name == "web_search":
                await say_while(ctx, "search")
                found = await self.llm.web_search(a.get("query", ""))
                ctx.setdefault("searched", []).append((a.get("query", ""), found))
                return found, "", []
            if name == "think_harder":
                return "already using the larger model", "", []
        except (KeyError, ValueError, TypeError) as e:
            return f"error: {e}", "", []
        return f"error: unknown tool {name}", "", []

    async def _week_weather(self) -> str:
        data = await self.weather.fetch()
        if not data:
            return "weather unavailable"
        d = data["daily"]
        rows = []
        for i, date in enumerate(d["time"]):
            day = DAY_NAMES[datetime.strptime(date, "%Y-%m-%d").weekday()]
            rows.append(f"{day}: {WMO.get(d['weather_code'][i], 'mixed')}, high {round(d['temperature_2m_max'][i])}, "
                        f"low {round(d['temperature_2m_min'][i])}, rain chance {d['precipitation_probability_max'][i]}%")
        return "\n".join(rows)
