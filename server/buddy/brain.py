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
from .llm import LLMError, parse_json_loose
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
    "retry": ["Hmm, couldn't find that straight off. Let me have a dig.", "Not finding it yet, give me a sec.",
              "Hang on, let me try that another way."],
    "search": ["One sec, I'll look that up.", "Give me a sec, I'll check.", "Hang on, let me have a look.",
               "Two secs, I'll find out.", "Let me check that for you."],
    "think": ["Right, let me have a proper think.", "Okay, give me a moment to think this through.",
              "Ooh, good one. Let me have a think.", "Leave it with me a sec."],
}


# A quick try that came back empty-handed: worth a proper go (other wordings, the web, thinking).
QUICK_FAIL = re.compile(r"\b(couldn'?t|can'?t|didn'?t|could not|cannot) (find|see|work out|get)\b|"
                        r"hasn'?t started|not sure how|no results|nothing (came up|found)", re.I)
# ...but not setup problems, which another try won't fix.
SETUP_PROBLEM = re.compile(r"isn'?t (set up|connected)|premium|my speaker|update buddy|settings|api key|"
                           r"spotify (open|isn'?t letting)|switched off", re.I)


_WORDS = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine"}
_PICKS = {"one": 1, "won": 1, "first": 1, "1": 1, "two": 2, "to": 2, "too": 2, "second": 2, "2": 2, "three": 3,
          "third": 3, "3": 3, "four": 4, "for": 4, "fourth": 4, "4": 4, "five": 5, "fifth": 5, "5": 5, "six": 6,
          "sixth": 6, "6": 6, "seven": 7, "7": 7, "eight": 8, "8": 8, "nine": 9, "9": 9}


def _number_pick(text: str) -> int | None:
    """ "two", "the second one", "number 2", "sound three please" -> 2, 2, 2, 3."""
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _PICKS and not (w in ("to", "too", "for") and len(text.split()) > 2):
            return _PICKS[w]
    return None


def quick_failed(text: str) -> bool:
    return bool(text and QUICK_FAIL.search(text) and not SETUP_PROBLEM.search(text))


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
    dropped: list[str] = field(default_factory=list)
    interrupted: str = ""  # the rest of an answer that got cut off, for "carry on"  # topics the user asked Buddy to leave alone

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
            "label": {"type": "string"},
            "sound": {"type": "integer", "description": "alarm sound number, only if the user said one"}},
            "required": ["time"]}}},
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
        "name": "get_weather", "description": "Weather forecast for any place (default: the user's home), for any "
                                              "day up to 10 days ahead, or a range of days.",
        "parameters": {"type": "object", "properties": {
            "place": {"type": "string", "description": "town or city, e.g. 'Paris' or 'Manchester'; empty = home"},
            "when": {"type": "string", "description": "e.g. 'today', 'tomorrow', 'friday', 'the weekend', "
                                                      "'in 5 days', 'this week', 'next 10 days', or YYYY-MM-DD"}}}}},
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
        "name": "go_quiet", "description": "The user wants you to stop, shut up, go away or turn off ENTIRELY "
                                           "(e.g. 'alright, that's me off to bed', 'leave it, I don't need you'). "
                                           "You go quiet straight away without saying anything.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "drop_topic", "description": "The user wants you to stop talking about ONE topic but carry on "
                                             "otherwise (e.g. 'enough about the weather', 'stop going on about my "
                                             "diet'). Remembered for the rest of the conversation.",
        "parameters": {"type": "object", "properties": {"topic": {"type": "string"}}, "required": ["topic"]}}},
    {"type": "function", "function": {
        "name": "switch_voice", "description": "Change the voice you speak with, among the user's saved voices. "
                                               "step 1 = next, -1 = previous; or a 1-based number; or a voice name.",
        "parameters": {"type": "object", "properties": {
            "step": {"type": "integer"}, "number": {"type": "integer"}, "name": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "study_progress", "description": "How the user's revising is going in Study Buddy: lessons recorded, "
                                                 "maths skills and their level (1-5), what's strong and weak, what to revise next. "
                                                 "Use for questions like 'how am I doing', 'what's my progress', 'what should I revise'.",
        "parameters": {"type": "object", "properties": {
            "person": {"type": "string", "description": "Only if they name someone else's profile"}}}}},
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
- Trust your earlier answers in this conversation, above all those your fact-checker confirmed (see the \
background notes): they were right, so never doubt or walk them back unless the user shows they were wrong. "(Background note ...)" lines are your own \
private notes about earlier turns (for example a web search you did); use them, never read them out, and don't \
apologise for or doubt earlier answers unless the user says they were wrong.
- If something goes wrong, be relaxed and kind about it, say what you can do instead, and keep it short.
- Be persistent but sensible: if a tool can't find something, try again a different way (another spelling, a \
web search for the right name, thinking it through) before giving up. Simple things that worked need no extra effort.
- Read what the user means, not just the words: wanting you to stop or go away entirely means go_quiet; wanting \
you to drop one subject ("enough about that") means drop_topic, then carry on normally."""


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
        self.sounds = None  # AlarmSounds, set by the hub
        self.missed: list[str] = []  # alarms that rang out with nobody there, to mention next time
        self.study = None  # (StudyStore, Practice), set by the hub

    def now(self) -> datetime:
        return datetime.now(ZoneInfo(self.settings["timezone"]))

    async def handle(self, text: str, conv: Conversation, ctx: dict | None = None) -> Reply:
        ctx = ctx or {}
        t0 = time.monotonic()
        text = intents.strip_wake(text)
        if not text:
            return Reply("", intent="empty")
        ctx["said"] = text
        reply = await self._pending(text, conv, ctx)
        if reply is None:
            intent = intents.match(text, self.now(), {"ringing": ctx.get("ringing"), "pager": conv.pager is not None})
            reply = await self._intent(intent, conv, ctx) if intent else None
            if reply is not None and quick_failed(reply.text):
                # Don't give up after one quick try: tell them, then have a proper go.
                await say_while(ctx, "retry")
                reply = await self._llm(text, conv, ctx, note=reply.text)
        if reply is None:
            reply = await self._llm(text, conv, ctx)
        if reply.intent in ("llm", "llm_deep") and reply.text and self.settings["fact_check"]:
            reply = await self._make_sure(text, conv, ctx, reply)
        reply.text = speakable(reply.text, 1400 if reply.intent == "llm_deep" else 900)
        if reply.text and reply.intent in REWORD and self.settings["reword_replies"]:
            reply.text = await self._reword(text, reply.text)
        if self.missed and reply.text and reply.intent not in ("off", "stop", "repeat", "think_offer"):
            reply.text = await self._mention_missed(text, reply.text)
        conv.add("user", text)
        if reply.text:
            notes = [f'I searched the web for "{q}" and found: {r[:350]}' for q, r in ctx.get("searched", [])]
            if ctx.get("thought"):
                notes.append("I thought this one through carefully.")
            if ctx.get("checked") == "correct":
                notes.append("My fact-checker confirmed this answer is correct, so I stand by it.")
            elif ctx.get("checked") == "gave_up":
                notes.append("I tried my own knowledge, a web search and thinking hard, and couldn't find solid info.")
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
        if p.get("intent") == "alarm_sound":
            n = _number_pick(text)
            names = self.sounds.names() if self.sounds else []
            if n and 1 <= n <= len(names):
                self.alarms.set_sound(p["id"], n)
                return Reply(random.choice([f"Sound {_WORDS[n]} it is.", f"Lovely, sound {_WORDS[n]}.",
                                            f"Done, sound {_WORDS[n]}."]), intent="alarm_sound")
            self.alarms.set_sound(p["id"], int(self.settings["default_alarm_sound"]))
            return None  # something else entirely: keep the usual sound and treat it as a new request
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
        if n == "carry_on":
            rest, conv.interrupted = conv.interrupted, ""
            if rest:
                return Reply(rest, intent="carry_on")
            return None  # nothing was cut off: let Claude work out what they mean
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
            return Reply(await self.weather.report(a.get("place", ""), a.get("when", a.get("day", "today"))), intent=n)
        if n == "timer":
            al = self.alarms.add_timer(a["seconds"], a.get("label", ""))
            return Reply(self.alarms.confirm_text(al), intent=n)
        if n == "alarm_in":
            self.alarms.add_timer(a["seconds"], "", kind="alarm")
            return Reply(f"Okay, I'll wake you in {human_duration(a['seconds'])}.", intent=n)
        if n == "alarm":
            al = self.alarms.add_alarm(a["time"], a.get("repeat") or None, a.get("label", ""), a.get("date"))
            ask = self._alarm_sound(al, conv, ctx.get("said", ""))
            if ask:
                return Reply(f"{self.alarms.confirm_text(al)} {ask}", intent="alarm_ask", follow_up=True)
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

    async def _mention_missed(self, said_by_user: str, reply: str) -> str:
        """Work "by the way, you missed your 7 AM alarm" into the reply wherever it fits best."""
        missed, self.missed = self.missed, []
        what = " and ".join(missed)
        plural = "alarms" if len(missed) > 1 else "alarm"
        fallback = f"{reply} By the way, you missed your {what} {plural}."
        system = ("You are a friendly voice assistant. Work one short, natural mention into the reply below that "
                  f"the user missed their {what} {plural} (it rang out with nobody there). Put it wherever it fits "
                  "best: at the start if it matters most, in the middle if it flows, or at the end as a 'by the "
                  "way'. Keep every other fact and number in the reply exactly. Reply with only the words to say.")
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f'They said: "{said_by_user}"\nReply: "{reply}"'}]
        try:
            res = await asyncio.wait_for(self.llm.chat(msgs, tier="fast", max_tokens=300), timeout=4.0)
            out = (res.content or "").strip().strip('"').strip()
        except Exception as e:  # noqa: BLE001
            log.info("missed-alarm mention fell back: %s", e)
            return fallback
        digits_ok = all(n in out for n in re.findall(r"\d+", reply))
        mentioned = "alarm" in out.lower() and "miss" in out.lower()
        return out if out and digits_ok and mentioned else fallback

    def _alarm_sound(self, al, conv: Conversation, said: str) -> str:
        """Pick the alarm's sound: the one they named, else ask "one or two?" when there's a choice.
        Returns the question to add to the reply, or ""."""
        names = self.sounds.names() if self.sounds else []
        if al.kind != "alarm" or not names:
            return ""
        m = re.search(r"\b(?:sound|tone|ringtone|ring)\s*(?:number\s*)?(\w+)", said or "", re.I)
        n = _number_pick(m.group(1)) if m else None
        if n and 1 <= n <= len(names):
            self.alarms.set_sound(al.id, n)
            return ""
        if len(names) == 1:
            self.alarms.set_sound(al.id, 1)
            return ""
        self.alarms.set_sound(al.id, int(self.settings["default_alarm_sound"]))  # until they answer
        conv.pending = {"intent": "alarm_sound", "id": al.id}
        options = [_WORDS[i] for i in range(1, min(len(names), 9) + 1)]
        return f"Which alarm sound, {', '.join(options[:-1])} or {options[-1]}?"

    async def _check(self, question: str, answer: str, ctx: dict) -> dict:
        """A strict, separate fact-checker. Returns {"verdict": correct | not_factual | unsure | wrong, "why": ...}."""
        evidence = "\n".join(f'- Web search for "{q}": {r[:1200]}' for q, r in ctx.get("searched", [])) or "(none)"
        now = self.now()
        system = ("You are a strict fact-checker for a voice assistant's spoken answers. Decide if the answer is "
                  "accurate. Reply with JSON only: {\"verdict\": \"correct\" | \"not_factual\" | \"unsure\" | "
                  "\"wrong\", \"why\": \"short reason\"}.\n"
                  "- not_factual: chat, opinions, jokes, feelings, confirmations of tasks, or asking the user something.\n"
                  "- correct: the facts are right: supported by the evidence, or settled general knowledge you are "
                  "confident about.\n"
                  "- unsure: it depends on recent or changing facts (news, scores, prices, people's current roles, "
                  "opening times, anything after your knowledge) and there is no evidence supporting it, or you "
                  "can't tell.\n"
                  "- wrong: it contradicts the evidence or well-established facts.\n"
                  f"Today is {now.strftime('%A %d %B %Y')}. Be strict about specifics (names, numbers, dates).")
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": f"Question: {question}\nAnswer: {answer}\nEvidence:\n{evidence}"}]
        try:
            res = await asyncio.wait_for(self.llm.chat(msgs, tier="fast", json_mode=True, max_tokens=300), timeout=8.0)
            data = parse_json_loose(res.content or "")
        except Exception as e:  # noqa: BLE001 - a broken check mustn't block the answer
            log.info("fact-check skipped: %s", e)
            return {"verdict": "not_factual", "why": "check unavailable"}
        if not isinstance(data, dict) or data.get("verdict") not in ("correct", "not_factual", "unsure", "wrong"):
            return {"verdict": "not_factual", "why": "unclear check"}
        return data

    async def _make_sure(self, text: str, conv: Conversation, ctx: dict, reply: Reply) -> Reply:
        """Check the answer before saying it. If it doesn't hold up: search, then think hard and search,
        checking each time; if nothing holds up, say so honestly rather than guess."""
        if reply.text.rstrip().endswith("?") or reply.intent == "think_offer":
            return reply  # asking the user something: nothing to check
        verdict = await self._check(text, reply.text, ctx)
        log.info("fact-check: %s (%s)", verdict.get("verdict"), verdict.get("why", ""))
        if verdict["verdict"] in ("correct", "not_factual"):
            ctx["checked"] = "correct" if verdict["verdict"] == "correct" else ""
            return reply
        say = ctx.get("say")
        if say:
            await say(random.choice(["Let me double-check that.", "Hang on, let me make sure that's right.",
                                     "One sec, I want to be sure about this."]))
            ctx["said_while"] = True
        for attempt in ("search", "think"):
            why = verdict.get("why", "")
            note = f"A fact-check said your answer \"{reply.text[:300]}\" was {verdict['verdict']}: {why}"
            if attempt == "think":
                ctx["thought"] = True
            retry = await self._llm(text, conv, ctx, tier="smart" if attempt == "think" else "fast",
                                    allow_think=False, note=note, force_search=True)
            if not retry.text:
                continue
            verdict = await self._check(text, retry.text, ctx)
            log.info("fact-check after %s: %s (%s)", attempt, verdict.get("verdict"), verdict.get("why", ""))
            if verdict["verdict"] in ("correct", "not_factual"):
                ctx["checked"] = "correct" if verdict["verdict"] == "correct" else ""
                retry.intent = "llm_deep" if attempt == "think" else retry.intent
                return retry
        ctx["checked"] = "gave_up"
        return Reply("Sorry, I've had a proper look, on the web and thinking it through, and I couldn't find solid "
                     "information on that. I'd rather not guess.", reply.actions, model=reply.model, intent="llm")

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
                return await sp.play()
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
            if str(e) == "NO_DEVICE":
                return "Spotify says there's nothing to play on just now. Give the Spotify app a tap and try again."
            return str(e)
        except Exception as e:  # noqa: BLE001 - say what happened instead of a vague "something went wrong"
            log.exception("Spotify request failed")
            sp.last_error = f"{type(e).__name__}: {e}"[:200]
            return f"Spotify tripped up there ({type(e).__name__}). Want me to have another go?"

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
                   allow_think: bool = True, note: str = "", force_search: bool = False) -> Reply:
        if THINK_REQUEST.search(text):
            tier = "smart"
        if tier == "smart":
            ctx["thought"] = True
            await say_while(ctx, "think")
        system = await self.system_prompt()
        if note:
            system += (f"\nYour quick first try at this just failed: \"{note}\". Be persistent: try another wording "
                       "or spelling, use web_search to pin down the right name if that would help, then use the tool "
                       "again. If it still doesn't work after a couple of goes, say briefly and kindly what you tried.")
        if force_search:
            system += ("\nYour last answer to this didn't pass a fact-check. Use web_search to get current, reliable "
                       "facts before answering, and only say what the results support.")
        if conv.dropped:
            system += ("\nTopics the user asked you to drop (don't bring them up again unless they do): "
                       + "; ".join(conv.dropped[-6:]) + ".")
        messages = [{"role": "system", "content": system}]
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
        for _ in range(6):  # room to try again when a tool comes back empty
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
                if name == "go_quiet":  # turn off without a word, like "off"
                    return Reply("", actions + [{"type": "stop"}, {"type": "sleep"}], model=model, intent="off")
                if name == "drop_topic":
                    topic = str(args.get("topic", "")).strip()[:80]
                    if topic and topic not in conv.dropped:
                        conv.dropped.append(topic)
                result, confirm, acts = await self._run_tool(name, args, conv, ctx) if name != "drop_topic" else (
                    f"Noted: leave '{args.get('topic', '')}' alone from now on. Acknowledge in a few words and move on.",
                    "", [])
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
                ask = self._alarm_sound(al, conv, f"sound {a['sound']}" if a.get("sound") else ctx.get("said", ""))
                if ask:
                    c = f"{c} {ask}"
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
                when = a.get("when") or a.get("day") or "today"
                return await self.weather.report(a.get("place", ""), when), "", []
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
                if quick_failed(said):
                    return said + " (Try a different search, or web_search to find the exact name.)", "", []
                return said or "done", said, []
            if name == "switch_voice":
                args = {k: a[k] for k in ("number", "name") if a.get(k)} or {"step": int(a.get("step") or 1)}
                r = self._switch_voice(args)
                return r.text, r.text, []
            if name == "study_progress":
                return self._study_progress(a.get("person", "")), "", []
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

    def _study_progress(self, person: str = "") -> str:
        """A plain summary of someone's revising, for Buddy to talk about."""
        if not self.study:
            return "Study Buddy isn't set up."
        store, practice = self.study
        profiles = store.profiles()
        if not profiles:
            return "Nobody has a Study Buddy profile yet (open Study from Buddy's web page)."
        want = (person or self.settings["user_name"] or "").strip().lower()
        p = (next((x for x in profiles if x["name"].lower() == want), None)
             or next((x for x in profiles if want and x["name"].lower().startswith(want.split()[0])), None)
             or next((x for x in profiles if x["id"] == store.recorder_owner()), None) or profiles[0])
        lessons = [x for x in store.lessons(p["id"]) if x["status"] == "ready"]
        skills = practice.progress.summary(p["id"])
        week = practice.progress.recent(p["id"])
        lines = [f"Profile: {p['name']}. {len(lessons)} lessons with notes."]
        for x in lessons[:5]:
            lines.append(f"- {x['date']}: {x['subject'] or 'lesson'}{(' about ' + x['about']) if x.get('about') else ''}")
        if skills:
            lines.append("Maths skills (level 1 = just starting, 5 = exam-ready; answered / right):")
            for k in skills:
                lines.append(f"- {k['name']}: level {k['level']} ({k['right']}/{k['seen']} right)")
            weakest = [k["name"] for k in skills[:2]]
            lines.append(f"Weakest: {', '.join(weakest)}.")
        else:
            lines.append("No maths questions answered yet.")
        lines.append(f"Last 7 days: {week['answered']} questions answered, {week['right']} right, on {week['days']} days.")
        return "\n".join(lines)

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
