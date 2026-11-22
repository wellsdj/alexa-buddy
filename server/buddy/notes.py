"""Things you ask Buddy to save: an ingredients list, a phone number, an idea. Kept on this computer."""
from __future__ import annotations

import json
import time
from pathlib import Path


class Notes:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "notes.json"

    def all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return []

    def _save(self, notes: list[dict]) -> None:
        self.path.write_text(json.dumps(notes, indent=2))

    def add(self, title: str, text: str) -> dict:
        notes = [n for n in self.all() if n["title"].lower() != title.strip().lower()]  # same title: replaced
        note = {"title": title.strip() or "Note", "text": text.strip(), "saved": time.strftime("%Y-%m-%d %H:%M")}
        notes.append(note)
        self._save(notes)
        return note

    def find(self, q: str) -> list[dict]:
        q = (q or "").lower().strip()
        return [n for n in self.all() if not q or q in n["title"].lower() or q in n["text"].lower()]

    def delete(self, q: str) -> int:
        gone = self.find(q)
        if q:
            self._save([n for n in self.all() if n not in gone])
        return len(gone) if q else 0
