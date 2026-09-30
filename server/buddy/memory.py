"""Long-term memory about the user.

- You can say "remember that...", "what's in your memory", "forget number 3",
  "change the one about my sister to...". Things you ask it to remember are pinned
  and never removed automatically.
- After each conversation the smart model extracts anything worth keeping.
- When the list grows, the smart model compacts it: merges duplicates and drops trivia.
  A backup is written first, and the result is rejected if it looks destructive.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("buddy.memory")

CATEGORIES = ("about_you", "people", "preferences", "routines", "plans", "other")


@dataclass
class Memory:
    id: int
    text: str
    category: str = "other"
    pinned: bool = False
    source: str = "learned"  # user | learned | merged
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


STOPWORDS = set("""a an the is are was were be been am i im i'm you your user users they them their he she his
her it its to of in on at for with and or but so that this these those my me we our has have had do does
did very really quite just also too as by from about""".split())


def similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def content_words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if w not in STOPWORDS}


def is_duplicate(a: str, b: str) -> bool:
    """Same fact, or one is a more detailed version of the other. Plain similarity isn't
    enough: "Sister is called Emma" vs "Sister is called Anna" are 90% alike but different."""
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return _norm(a) == _norm(b)
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return small <= big and (len(small) >= 2 or small == big)


class MemoryStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._items: dict[int, Memory] = {}
        self._next_id = 1
        self.last_compaction = 0.0
        self._listeners: list = []
        self._load()

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def _changed(self) -> None:
        for fn in list(self._listeners):
            fn()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            broken = self.path.with_suffix(f".broken-{int(time.time())}.json")
            shutil.copy(self.path, broken)
            log.error("memory file was corrupt; copied to %s", broken)
            return
        for item in raw.get("items", []):
            m = Memory(**{k: v for k, v in item.items() if k in Memory.__dataclass_fields__})
            self._items[m.id] = m
        self._next_id = max([raw.get("next_id", 1)] + [m.id + 1 for m in self._items.values()])
        self.last_compaction = raw.get("last_compaction", 0.0)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "next_id": self._next_id,
            "last_compaction": self.last_compaction,
            "items": [asdict(m) for m in sorted(self._items.values(), key=lambda m: m.id)],
        }, indent=2))
        tmp.replace(self.path)

    def backup(self, tag: str) -> Path | None:
        if not self.path.exists():
            return None
        dest = self.path.parent / "memory_backups" / f"memory-{time.strftime('%Y%m%d-%H%M%S')}-{tag}.json"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.path, dest)
        backups = sorted(dest.parent.glob("memory-*.json"))
        for old in backups[:-30]:
            old.unlink(missing_ok=True)
        return dest

    # ---------- reads ----------
    def list(self) -> list[Memory]:
        with self._lock:
            return sorted(self._items.values(), key=lambda m: m.id)

    def get(self, mid: int) -> Memory | None:
        with self._lock:
            return self._items.get(int(mid))

    def __len__(self) -> int:
        return len(self._items)

    def total_chars(self) -> int:
        return sum(len(m.text) for m in self._items.values())

    def find_similar(self, text: str) -> Memory | None:
        matches = [m for m in self.list() if is_duplicate(m.text, text)]
        return max(matches, key=lambda m: similarity(m.text, text)) if matches else None

    def render_for_prompt(self, max_chars: int = 1800) -> str:
        items = self.list()
        if not items:
            return "(nothing yet)"
        lines = [f"#{m.id} {m.text}" for m in items]
        text = "\n".join(lines)
        if len(text) <= max_chars:
            return text
        # Too long: pinned first, then the most recently updated.
        ordered = sorted(items, key=lambda m: (not m.pinned, -m.updated))
        out, used = [], 0
        for m in ordered:
            line = f"#{m.id} {m.text}"
            if used + len(line) + 1 > max_chars:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(out) + f"\n(+{len(items) - len(out)} more not shown)"

    def spoken_list(self, start: int = 0, page: int = 6) -> tuple[str, int | None]:
        items = self.list()
        if not items:
            return "My memory's empty right now. Tell me things like \"remember that my sister is called Emma\".", None
        chunk = items[start:start + page]
        intro = ""
        if start == 0:
            n = len(items)
            intro = f"I remember {n} thing{'s' if n != 1 else ''} about you. "
        body = " ".join(f"Number {m.id}: {m.text.rstrip('.')}." for m in chunk)
        nxt = start + page if start + page < len(items) else None
        tail = " Want me to keep going?" if nxt else " You can tell me to forget or change any of these by number."
        return intro + body + tail, nxt

    # ---------- writes ----------
    def add(self, text: str, category: str = "other", pinned: bool = False, source: str = "learned") -> tuple[Memory, bool]:
        """Returns (memory, created). Near-duplicates update the existing memory instead."""
        text = text.strip().rstrip(".") + "."
        category = category if category in CATEGORIES else "other"
        with self._lock:
            dup = self.find_similar(text)
            if dup:
                if len(text) > len(dup.text) or pinned:
                    dup.text = text
                dup.pinned = dup.pinned or pinned
                dup.updated = time.time()
                self._save()
                self._changed()
                return dup, False
            m = Memory(id=self._next_id, text=text, category=category, pinned=pinned, source=source)
            self._next_id += 1
            self._items[m.id] = m
            self._save()
        self._changed()
        return m, True

    def update(self, mid: int, text: str | None = None, category: str | None = None,
               pinned: bool | None = None) -> Memory | None:
        with self._lock:
            m = self._items.get(int(mid))
            if not m:
                return None
            if text:
                m.text = text.strip().rstrip(".") + "."
            if category in CATEGORIES:
                m.category = category
            if pinned is not None:
                m.pinned = pinned
            m.updated = time.time()
            self._save()
        self._changed()
        return m

    def delete(self, ids: list[int]) -> list[Memory]:
        removed = []
        with self._lock:
            for i in ids:
                m = self._items.pop(int(i), None)
                if m:
                    removed.append(m)
            if removed:
                self._save()
        if removed:
            self._changed()
        return removed

    def clear(self) -> int:
        with self._lock:
            self.backup("before-clear")
            n = len(self._items)
            self._items.clear()
            self._save()
        self._changed()
        return n

    def replace_all(self, items: list[Memory]) -> None:
        with self._lock:
            self._items = {m.id: m for m in items}
            self._next_id = max([self._next_id] + [m.id + 1 for m in items])
            self.last_compaction = time.time()
            self._save()
        self._changed()


EXTRACT_PROMPT = """You maintain the long-term memory of a voice assistant called {name}, about its one user.
Read the conversation and decide what is worth remembering for future conversations.

Keep: the user's name, people in their life (names, relationships), likes and dislikes, habits and routines, \
job/school/projects, plans with dates, important facts they shared, and corrections they made.
Ignore: one-off questions, weather, time, alarms, small talk, anything the assistant said, and anything \
temporary without lasting value.

Write each memory as one short third-person statement, e.g. "Prefers tea to coffee." or \
"Sister is called Emma and lives in Leeds." Never add something that is already in the existing memories; \
use "update" to improve an existing one instead. Only "delete" when the user retracted or contradicted it.
If nothing is worth remembering, return empty lists.

Categories: about_you, people, preferences, routines, plans, other.
Reply with JSON only: {{"add":[{{"text":"...","category":"..."}}],"update":[{{"id":1,"text":"..."}}],"delete":[{{"id":1}}]}}"""

COMPACT_PROMPT = """You are tidying the long-term memory of a voice assistant about its user.
Rewrite the list so it is shorter and more useful:
- merge duplicates and near-duplicates into one clear statement,
- drop trivial, stale or one-off items (past events, things that no longer matter),
- keep every item marked PINNED (you may merge its wording with related items, but keep its meaning),
- keep statements short, third person, one fact each where possible.
Reply with JSON only: {"memories":[{"text":"...","category":"...","from":[ids it came from]}],"dropped":[ids]}"""


class MemoryManager:
    """Learning and compaction on top of the store; uses the smart model."""

    def __init__(self, store: MemoryStore, llm, settings):
        self.store = store
        self.llm = llm
        self.settings = settings
        self.last_status = ""

    async def learn_from(self, transcript: list[dict]) -> dict:
        from .llm import LLMError, parse_json_loose
        user_turns = [t["content"] for t in transcript if t["role"] == "user"]
        if not user_turns or sum(len(u) for u in user_turns) < 25:
            return {"skipped": "too short"}
        convo = "\n".join(f"{'User' if t['role'] == 'user' else 'Assistant'}: {t['content']}"
                          for t in transcript if t.get("content"))[-6000:]
        messages = [
            {"role": "system", "content": EXTRACT_PROMPT.format(name=self.settings["assistant_name"])},
            {"role": "user", "content": f"Existing memories:\n{self.store.render_for_prompt(3000)}\n\nConversation:\n{convo}"},
        ]
        try:
            res = await self.llm.chat(messages, tier="smart", json_mode=True, max_tokens=700, temperature=0.2)
        except LLMError as e:
            self.last_status = f"learning skipped: {e}"
            return {"error": str(e)}
        data = parse_json_loose(res.content) or {}
        added, updated, deleted = [], [], []
        for item in data.get("add", []) or []:
            if isinstance(item, dict) and item.get("text"):
                m, created = self.store.add(item["text"], item.get("category", "other"), source="learned")
                (added if created else updated).append(m.text)
        for item in data.get("update", []) or []:
            if isinstance(item, dict) and item.get("id") and item.get("text"):
                m = self.store.update(int(item["id"]), item["text"])
                if m:
                    updated.append(m.text)
        for item in data.get("delete", []) or []:
            mid = item.get("id") if isinstance(item, dict) else item
            m = self.store.get(int(mid)) if str(mid).isdigit() else None
            if m and not m.pinned:
                deleted += [x.text for x in self.store.delete([m.id])]
        self.last_status = f"learned: +{len(added)} ~{len(updated)} -{len(deleted)} ({res.model})"
        if self.needs_compaction():
            await self.compact()
        return {"added": added, "updated": updated, "deleted": deleted}

    def needs_compaction(self) -> bool:
        if time.time() - self.store.last_compaction < 6 * 3600:
            return False
        return len(self.store) > 45 or self.store.total_chars() > 3500

    async def compact(self, force: bool = False) -> dict:
        from .llm import LLMError, parse_json_loose
        items = self.store.list()
        if len(items) < 4 and not force:
            return {"skipped": "nothing to compact"}
        listing = "\n".join(f"#{m.id} [{m.category}]{' PINNED' if m.pinned else ''} {m.text}" for m in items)
        messages = [{"role": "system", "content": COMPACT_PROMPT}, {"role": "user", "content": listing}]
        try:
            res = await self.llm.chat(messages, tier="smart", json_mode=True, max_tokens=1800, temperature=0.1)
        except LLMError as e:
            self.last_status = f"compaction skipped: {e}"
            return {"error": str(e)}
        data = parse_json_loose(res.content) or {}
        new_raw = [x for x in data.get("memories", []) or [] if isinstance(x, dict) and x.get("text")]
        if not new_raw:
            self.last_status = "compaction returned nothing; kept memory as is"
            return {"error": "empty result"}
        by_id = {m.id: m for m in items}
        covered: set[int] = set()
        result: list[Memory] = []
        for x in new_raw:
            sources = [int(i) for i in x.get("from", []) if str(i).isdigit() and int(i) in by_id]
            covered.update(sources)
            pinned = any(by_id[i].pinned for i in sources)
            keep_id = min(sources) if sources else None
            if keep_id is not None and keep_id in {m.id for m in result}:
                keep_id = None
            created = min((by_id[i].created for i in sources), default=time.time())
            result.append(Memory(id=keep_id or 0, text=x["text"].strip().rstrip(".") + ".",
                                 category=x.get("category") if x.get("category") in CATEGORIES else "other",
                                 pinned=pinned, source="merged" if len(sources) > 1 else "learned",
                                 created=created))
        # Pinned memories are never allowed to vanish.
        for m in items:
            if m.pinned and m.id not in covered:
                result.append(m)
        unpinned_before = sum(1 for m in items if not m.pinned)
        unpinned_after = sum(1 for m in result if not m.pinned)
        if unpinned_before >= 8 and unpinned_after < unpinned_before * 0.35 and not force:
            self.last_status = "compaction looked too destructive; kept memory as is"
            return {"error": "too destructive", "before": len(items), "after": len(result)}
        self.store.backup("before-compaction")
        next_id = max([m.id for m in items] + [0]) + 1
        for m in result:
            if not m.id:
                m.id = next_id
                next_id += 1
        self.store.replace_all(result)
        self.last_status = f"compacted {len(items)} -> {len(result)} ({res.model})"
        return {"before": len(items), "after": len(result)}
