"""Website logins Buddy may use when it's doing a job online.

Saved only on this computer (data/web_logins.json, readable by you alone). Buddy fills them straight into
the page: the password never goes to Claude, the chat or the logs.
"""
from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import urlparse


def site_of(text: str) -> str:
    """'https://www.clubspark.lta.org.uk/x' or 'clubspark.lta.org.uk' -> 'clubspark.lta.org.uk'."""
    t = (text or "").strip().lower()
    host = urlparse(t if "://" in t else "https://" + t).hostname or ""
    return re.sub(r"^www\.", "", host)


class LoginStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "web_logins.json"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list(self) -> list[dict]:
        """Sites and usernames only (never passwords)."""
        return [{"site": k, "username": v.get("username", "")} for k, v in sorted(self._read().items())]

    def save(self, site: str, username: str, password: str) -> str:
        s = site_of(site)
        if not s or not password:
            raise ValueError("Give the website and the password.")
        with self._lock:
            data = self._read()
            data[s] = {"username": username.strip(), "password": password}
            self._write(data)
        return s

    def remove(self, site: str) -> None:
        with self._lock:
            data = self._read()
            data.pop(site_of(site), None)
            self._write(data)

    def find(self, url: str) -> tuple[str, dict] | None:
        """The saved login for this page: the same site, or one it's part of (login.example.com -> example.com)."""
        host = site_of(url)
        data = self._read()
        best = None
        for s, v in data.items():
            if host == s or host.endswith("." + s) or s.endswith("." + host):
                if best is None or len(s) > len(best[0]):
                    best = (s, v)
        return best
