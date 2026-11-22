"""Bugs we actually hit, pinned down so they can't quietly come back."""
import sys
import types
from datetime import datetime
from pathlib import Path

import pytest

from buddy import intents
from buddy.logins import LoginStore, group_of

NOW = datetime(2026, 11, 22, 19, 0)


# "help me cook pasta" was answered with the list of things Buddy can do.
@pytest.mark.parametrize("text", ["help me cook pasta", "can you help me with my homework", "help me find a recipe"])
def test_help_with_something_is_a_request(text):
    m = intents.match(text, NOW)
    assert m is None or m.name != "help"


@pytest.mark.parametrize("text", ["help", "what can you do", "help me"])
def test_help_on_its_own_is_help(text):
    assert intents.match(text, NOW).name == "help"


# Two answers ran together: "I'm sure.Right, ..." was spoken as one blurred sentence.
def test_sentences_split_without_a_space():
    from buddy import speech
    cls = next(v for v in vars(speech).values() if isinstance(v, type) and hasattr(v, "feed") and hasattr(v, "flush")
               and "sentence" in (v.__doc__ or "").lower())
    sp = cls()
    out = sp.feed("I'm sure.Right, here's the next bit. ") + sp.flush()
    assert out[0].strip() == "I'm sure."
    assert any(s.strip().startswith("Right") for s in out[1:])


# Logins: the school email was used for tennis; LTA/ClubSpark/Pensford are one login.
def _store(tmp_path):
    st = LoginStore(tmp_path)
    st.save_account("school email", "wthompson24@school.example.sch.uk", "s")
    st.save_account("home email", "wells@example.com", "h")
    return st


def test_school_email_never_used_for_home_jobs(tmp_path):
    st = _store(tmp_path)
    assert st.pick(school=False)[0] == "home email"
    assert st.pick(school=True)[0] == "school email"


def test_sign_in_page_names_the_account(tmp_path):
    st = _store(tmp_path)
    assert st.account_on_page("Hi Wells\nwthompson24@school.example.sch.uk\nEnter your password")[0] == "school email"
    assert st.account_on_page("Choose an account") is None


@pytest.mark.parametrize("url", ["https://www.lta.org.uk/login", "https://clubspark.lta.org.uk/PensfordTennisClub",
                                 "https://pensfordtennis.example"])
def test_tennis_sites_share_one_login(tmp_path, url):
    st = LoginStore(tmp_path)
    st.save("https://clubspark.lta.org.uk", "me@example.com", "pw")
    assert group_of(url) == "tennis"
    assert st.find(url)[1]["username"] == "me@example.com"


def test_passwords_never_listed(tmp_path):
    st = _store(tmp_path)
    st.save("https://example.com", "me", "secret")
    assert "secret" not in repr(st.list()) and "s" not in [a.get("password") for a in st.account_list()]


# Buddy's browser crashed on Linux when --no-sandbox was dropped everywhere: only on the Mac.
def test_no_sandbox_only_dropped_on_mac():
    src = (Path(__file__).resolve().parents[1] / "buddy" / "online.py").read_text()
    line = next(ln for ln in src.splitlines() if '"--no-sandbox"' in ln)
    assert 'sys.platform == "darwin"' in line


# USB setup re-sent the address every half second: the board reconnected ~30 times, playing the
# start-up sound each time.
class _FakeSerial:
    writes: list = []
    answer = True

    def __init__(self):
        self.port = self.baudrate = self.timeout = None
        self.dtr = self.rts = False

    def open(self):
        pass

    def close(self):
        pass

    def write(self, b):
        _FakeSerial.writes.append(b.decode())

    def readline(self):
        import time
        time.sleep(0.05)
        return b"[usb] ok\n" if (_FakeSerial.answer and _FakeSerial.writes) else b""


@pytest.mark.parametrize("answers", [True, False])
def test_usb_setup_sends_the_address_once(monkeypatch, answers):
    from buddy import usb_setup
    _FakeSerial.writes, _FakeSerial.answer = [], answers
    monkeypatch.setitem(sys.modules, "serial", types.SimpleNamespace(Serial=_FakeSerial))
    monkeypatch.setattr(usb_setup, "board_ports", lambda: ["/dev/cu.usbmodemTEST"])
    monkeypatch.setattr(usb_setup, "my_address", lambda: "192.168.1.20")
    usb_setup.send_setup("", "", 8000, wait_s=4.5)
    host_lines = [w for w in _FakeSerial.writes if w.startswith("BUDDY host")]
    assert len(host_lines) <= (1 if answers else 2)  # at most once more after one restart


# The start-up sound played every time the board reconnected, not once per switch-on.
def test_startup_sound_once_per_switch_on(monkeypatch):
    from buddy import hub as hubmod
    hub = hubmod.Hub.__new__(hubmod.Hub)
    hub.startup_played, hub.device_gone_at = {}, {}
    clock = {"t": 1000.0}
    monkeypatch.setattr(hubmod.time, "time", lambda: clock["t"])
    s = types.SimpleNamespace(name="desk", info={})
    played = 0
    for up in (5, 8, 12, 30):  # the board reconnecting a few times while it starts up
        clock["t"] = 1000.0 + up
        s.info = {"uptime": up}
        played += hub.should_play_startup(s)
    assert played == 1
    clock["t"] = 5000.0  # switched off and on again later: plays again
    s.info = {"uptime": 3}
    assert hub.should_play_startup(s)


# Buddy kept asking "is it week A or B?": it's built in. The week of Monday 23 Nov 2026 is week B.
def test_knows_week_b_out_of_the_box(tmp_path):
    from datetime import date

    from buddy.timetable import Timetable
    tt = Timetable(tmp_path)
    assert tt.week_of(date(2026, 11, 23)) == "B"
    assert tt.week_of(date(2026, 11, 30)) == "A"
    assert tt.spoken(date(2026, 11, 23)).startswith("Monday, week B. Form first. Period 1,")
    for word in ("room", "Mr ", "Mrs ", "Ms ", ":"):  # subjects only: no times, rooms or teachers
        assert word not in tt.spoken(date(2026, 11, 23))
