"""Settings: defaults, .env keys, and UI-editable overrides saved to data/settings.json."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = ROOT / "models"
WEB_DIR = ROOT / "web"
DATA_DIR = Path(os.environ.get("BUDDY_DATA_DIR", ROOT / "data"))

load_dotenv(ROOT / ".env")

# Groq retires models regularly (Llama left the free tier on 2026-08-16), so these are
# preferences, not hard requirements: llm.py checks what the account can actually see.
DEFAULTS: dict[str, Any] = {
    "assistant_name": "Buddy",
    "user_name": "",
    "location_name": "Richmond, London",
    "latitude": 51.4613,
    "longitude": -0.3037,
    "timezone": "Europe/London",
    "temperature_unit": "celsius",
    "fast_model": "openai/gpt-oss-20b",
    "smart_model": "openai/gpt-oss-120b",
    "fallback_models": ["qwen/qwen3.6-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"],
    "stt_model": "whisper-large-v3-turbo",
    "tts_engine": "auto",  # auto | elevenlabs | piper | browser
    "elevenlabs_voice_id": "JBFqnCBsd6RMkjVDRZzb",  # "George": warm British male, a premade voice
    "elevenlabs_model": "eleven_flash_v2_5",
    "voice_speed": 1.0,
    "piper_voice": "",  # path to a Piper .onnx voice; empty = first one found in data/voices
    "wake_threshold": 0.5,
    "wake_verify_threshold": 0.15,
    "follow_up": "questions",  # off | questions | always
    "end_of_speech_ms": 800,
    "max_listen_s": 15,
    "snooze_minutes": 9,
    "morning_briefing": True,
    "volume": 6,
    "device_mic_shift": 14,  # desk unit mic loudness: lower = louder (each step doubles it)
    "device_token": "",
}

SECRET_KEYS = ("GROQ_API_KEY", "ELEVENLABS_API_KEY")


class Settings:
    def __init__(self, data_dir: Path = DATA_DIR):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._path = self.data_dir / "settings.json"
        self._secrets_path = self.data_dir / "secrets.json"
        self._lock = threading.RLock()
        self._values = copy.deepcopy(DEFAULTS)
        self._secrets: dict[str, str] = {}
        self._listeners: list = []
        self._load()

    def _load(self) -> None:
        for path, target in ((self._path, self._values), (self._secrets_path, self._secrets)):
            if path.exists():
                try:
                    stored = json.loads(path.read_text())
                except json.JSONDecodeError:
                    continue
                for k, v in stored.items():
                    if target is self._secrets or k in DEFAULTS:
                        target[k] = v

    def __getitem__(self, key: str) -> Any:
        with self._lock:
            return self._values[key]

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._values.get(key, default)

    def all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._values)

    def update(self, changes: dict[str, Any]) -> dict[str, Any]:
        applied = {}
        with self._lock:
            for k, v in changes.items():
                if k not in DEFAULTS:
                    continue
                default = DEFAULTS[k]
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, float):
                    v = float(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, list) and isinstance(v, str):
                    v = [s.strip() for s in v.split(",") if s.strip()]
                self._values[k] = v
                applied[k] = v
            self._path.write_text(json.dumps(
                {k: v for k, v in self._values.items() if v != DEFAULTS[k]}, indent=2))
        for fn in list(self._listeners):
            fn(applied)
        return applied

    def on_change(self, fn) -> None:
        self._listeners.append(fn)

    def secret(self, name: str) -> str:
        env = os.environ.get(name, "").strip()
        if env:
            return env
        with self._lock:
            return self._secrets.get(name, "").strip()

    def set_secret(self, name: str, value: str) -> None:
        if name not in SECRET_KEYS:
            raise KeyError(name)
        with self._lock:
            if value:
                self._secrets[name] = value.strip()
            else:
                self._secrets.pop(name, None)
            self._secrets_path.write_text(json.dumps(self._secrets, indent=2))
            try:
                os.chmod(self._secrets_path, 0o600)
            except OSError:
                pass
        for fn in list(self._listeners):
            fn({name: "***"})

    def secret_status(self) -> dict[str, dict[str, Any]]:
        out = {}
        for name in SECRET_KEYS:
            source = "env" if os.environ.get(name, "").strip() else (
                "saved" if self._secrets.get(name) else "missing")
            out[name] = {"set": source != "missing", "source": source}
        return out
