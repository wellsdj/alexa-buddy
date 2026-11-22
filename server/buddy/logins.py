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


# Sites that share one login: the same account works on all of them, saved once under the group's name.
GROUPS = {
    "tennis": re.compile(r"lta\.org\.uk|clubspark|pensford|tennis", re.I),
}


def group_of(text: str) -> str:
    for name, rx in GROUPS.items():
        if rx.search(text or ""):
            return name
    return ""


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
        """By name ("school email"), by email, by a word in either ("school"), or the only one there is."""
        q = (label_or_email or "").strip().lower()
        accts = self.accounts()
        for k, v in accts.items():
            if q in (k, v.get("email", "").lower()) or (q and q in k):
                return k, v
        words = [w for w in re.findall(r"[a-z0-9]+", q) if w not in ("email", "account", "my", "the", "login")]
        for k, v in accts.items():
            hay = f"{k} {v.get('email', '')}".lower()
            if words and all(w in hay for w in words):
                return k, v
        if len(accts) == 1:
            return next(iter(accts.items()))
        return None

    @staticmethod
    def is_school(label: str, acct: dict) -> bool:
        return bool(re.search(r"school|bromcom|\.sch\.|\.school\b|\.ac\.uk", f"{label} {acct.get('email', '')}", re.I))

    def pick(self, school: bool) -> tuple[str, dict] | None:
        """The account for this kind of job: the school one for school things, otherwise the home one
        (never the school email for anything else)."""
        accts = [(k, v) for k, v in self.accounts().items() if self.is_school(k, v) == school]
        if not school:
            accts.sort(key=lambda kv: 0 if re.search(r"home|personal|my email", kv[0], re.I) else 1)
        return accts[0] if accts else None

    def account_on_page(self, page_text: str) -> tuple[str, dict] | None:
        """A sign-in page that shows which account it's for ("Hi Wells, wthompson24@..."): that one."""
        t = (page_text or "").lower()
        for k, v in self.accounts().items():
            if v.get("email") and v["email"].lower() in t:
                return k, v
        return None

    def list(self) -> list[dict]:
        """Sites and usernames only (never passwords)."""
        return [{"site": k, "username": v.get("username", "")} for k, v in sorted(self._read().items())]

    def save(self, site: str, username: str, password: str) -> str:
        s = site_of(site)
        g = group_of(s)
        if g:
            s = g  # one login for the whole group (e.g. "tennis")
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
        """The saved login for this page: by its site, or by its group ("tennis": LTA, ClubSpark, Pensford all
        one login)."""
        found = self._find_site(url)
        if found:
            return found
        group = group_of(site_of(url))
        if group:
            for s, v in self._read().items():  # a login saved for another site in the same group
                if group_of(s) == group:
                    return s, v
            for label, v in self.accounts().items():  # an account named after the group ("tennis", "LTA")
                if group_of(label) == group:
                    return label, {"username": v.get("email", ""), "password": v.get("password", "")}
        return None

    def _find_site(self, url: str) -> tuple[str, dict] | None:
        """The saved login for this page: the same site, or one it's part of (login.example.com -> example.com)."""
        host = site_of(url)
        data = self._read()
        best = None
        for s, v in data.items():
            if host == s or host.endswith("." + s) or s.endswith("." + host):
                if best is None or len(s) > len(best[0]):
                    best = (s, v)
        if best is None:  # an account named after the site ("bromcomvle.com")
            for label, v in self.accounts().items():
                s = site_of(label) if "." in label else ""
                if s and (host == s or host.endswith("." + s)):
                    return s, {"username": v.get("email", ""), "password": v.get("password", "")}
        return best
