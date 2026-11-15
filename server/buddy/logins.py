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
        self.accounts_path = Path(data_dir) / "web_accounts.json"
        self._lock = threading.Lock()

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict, path: Path | None = None) -> None:
        path = path or self.path
        path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ---------- accounts: "school email", "home email"... (an email/username and its password) ----------
    def accounts(self) -> dict:
        try:
            return json.loads(self.accounts_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}

    def account_list(self, passwords: bool = False) -> list[dict]:
        return [{"label": k, "email": v.get("email", ""), **({"password": v.get("password", "")} if passwords else
                                                              {"has_password": bool(v.get("password"))})}
                for k, v in sorted(self.accounts().items())]

    def save_account(self, label: str, email: str = "", password: str = "") -> str:
        label = re.sub(r"\s+", " ", (label or "").strip().lower())
        if not label:
            raise ValueError("Give the account a name, like school email.")
        with self._lock:
            data = self.accounts()
            old = data.get(label, {})
            data[label] = {"email": email.strip() or old.get("email", ""), "password": password or old.get("password", "")}
            self._write(data, self.accounts_path)
        return label

    def remove_account(self, label: str) -> None:
        with self._lock:
            data = self.accounts()
            data.pop((label or "").strip().lower(), None)
            self._write(data, self.accounts_path)

    def account(self, label_or_email: str) -> tuple[str, dict] | None:
        q = (label_or_email or "").strip().lower()
        for k, v in self.accounts().items():
            if q in (k, v.get("email", "").lower()) or (q and q in k):
                return k, v
        return None

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
