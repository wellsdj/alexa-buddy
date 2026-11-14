"""How long each part of a spoken turn takes, from the end of your speech to Buddy's first sound.

One TurnTimer per turn. It travels with the turn through asyncio's context (a ContextVar), so the
code that does each stage can mark it without the timer being passed around. Each finished turn
prints one line in the server window, goes in the Activity log, and is added to
data/timings.jsonl. `python -m buddy.timing_report` sums up the last 10 turns.
"""
from __future__ import annotations

import contextvars
import json
import logging
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("buddy.timing")

current: contextvars.ContextVar["TurnTimer | None"] = contextvars.ContextVar("buddy_turn_timer", default=None)

# The stages in the order they happen. "think" is everything between the transcript and the
# voice request that isn't one of the named Claude calls (quick commands, building the prompt).
STAGES = ("wait", "ears", "think", "haiku", "check", "reword", "search", "voice", "hold", "total")
LABELS = {
    "wait": "end-of-speech wait", "ears": "transcribe", "think": "other thinking",
    "haiku": "Haiku answer", "check": "fact-check", "reword": "reword", "search": "web search",
    "voice": "voice first byte", "hold": "hold before playing", "total": "TOTAL",
}


class TurnTimer:
    _count = 0

    def __init__(self, speech_end: float, endpoint: float):
        TurnTimer._count += 1
        self.n = TurnTimer._count
        self.marks: dict[str, float] = {"speech_end": speech_end, "endpoint": endpoint}
        self.spans: list[tuple[str, float, float]] = []
        self.info: dict = {}
        self.text = ""
        self.done = False

    def mark(self, name: str, at: float | None = None) -> None:
        self.marks.setdefault(name, time.monotonic() if at is None else at)  # the first time counts

    def stages(self) -> dict:
        m = self.marks
        ms = lambda a, b: int(round((m[b] - m[a]) * 1000)) if a in m and b in m else None  # noqa: E731
        out = {"wait": ms("speech_end", "endpoint"), "ears": ms("endpoint", "stt_done")}
        sums: dict[str, float] = {}
        counts: dict[str, int] = {}
        for label, a, b in self.spans:
            sums[label] = sums.get(label, 0.0) + (b - a)
            counts[label] = counts.get(label, 0) + 1
        for k in ("haiku", "check", "reword", "search"):
            out[k] = int(round(sums[k] * 1000)) if k in sums else None
        brain = ms("stt_done", "tts_sent")
        if brain is not None:
            out["think"] = max(0, brain - sum(v for k, v in out.items() if k in ("haiku", "check", "reword", "search")
                                               and v is not None))
        else:
            out["think"] = None
        out["voice"] = ms("tts_sent", "tts_first_byte")
        out["hold"] = ms("tts_first_byte", "first_audio")
        out["total"] = ms("speech_end", "first_audio")
        out["haiku_calls"] = counts.get("haiku", 0)
        return out

    def line(self, st: dict) -> str:
        parts = [f"TURN {self.n}"]
        for k in STAGES:
            v = st.get(k)
            if v is None:
                continue
            extra = f" ({st['haiku_calls']} calls)" if k == "haiku" and st.get("haiku_calls", 0) > 1 else ""
            parts.append(f"{LABELS[k]} {v}{extra}")
        return " | ".join(parts) + (f' | "{self.text[:40]}"' if self.text else "")

    def finish(self, data_dir: Path | None = None, record=None) -> dict | None:
        if self.done:
            return None
        self.done = True
        st = self.stages()
        if st["total"] is None and st["ears"] is None:
            return None  # nothing measured (a silent check that went nowhere)
        row = {"t": time.time(), "turn": self.n, "text": self.text[:80], **st, **self.info,
               "marks_ms": {k: int(round((v - self.marks["speech_end"]) * 1000)) for k, v in self.marks.items()}}
        log.info("%s", self.line(st))
        if record:
            record("timing", **{k: v for k, v in row.items() if k not in ("t", "marks_ms")})
        if data_dir:
            try:
                with (Path(data_dir) / "timings.jsonl").open("a") as f:
                    f.write(json.dumps(row) + "\n")
            except OSError:
                pass
        return row


# ---------- helpers that do nothing when no turn is being timed ----------
def mark(name: str) -> None:
    t = current.get()
    if t:
        t.mark(name)


def note(key: str, value) -> None:
    t = current.get()
    if t:
        if isinstance(value, (int, float)) and isinstance(t.info.get(key), (int, float)):
            t.info[key] += value
        else:
            t.info[key] = value


@contextmanager
def span(label: str):
    t = current.get()
    start = time.monotonic()
    if t and label == "haiku":
        t.mark("haiku_sent", start)
    try:
        yield
    finally:
        if t:
            end = time.monotonic()
            t.spans.append((label, start, end))
            if label == "haiku":
                # Not streamed yet: the first word only arrives with the whole answer.
                t.mark("haiku_first_token", end)
                t.mark("first_sentence", end)


# ---------- report ----------
def recent(path: Path, last: int = 20) -> list[dict]:
    """The last spoken turns, newest first (for the Speed tab)."""
    rows = []
    if path.exists():
        for line in path.read_text().splitlines()[-max(last * 3, 50):]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("total") is not None:
                r.pop("marks_ms", None)
                rows.append(r)
    return rows[-last:][::-1]


def report(path: Path, last: int = 10) -> str:
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    rows = [r for r in rows if r.get("total") is not None][-last:]
    if not rows:
        return "No timed turns yet. Talk to Buddy, then run this again."
    out = [f"Last {len(rows)} spoken turns (milliseconds):", f"{'stage':<22}{'middle':>8}{'average':>9}{'worst':>8}"]
    for k in STAGES:
        vals = [r[k] for r in rows if r.get(k) is not None]
        if not vals:
            continue
        out.append(f"{LABELS[k]:<22}{int(statistics.median(vals)):>8}{int(statistics.mean(vals)):>9}{max(vals):>8}"
                   + (f"   (in {len(vals)} of {len(rows)} turns)" if len(vals) < len(rows) else ""))
    return "\n".join(out)


def main() -> None:
    from .config import DATA_DIR
    last = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 10
    print(report(DATA_DIR / "timings.jsonl", last))


if __name__ == "__main__":
    main()
