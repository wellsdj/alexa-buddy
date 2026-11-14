"""Turn timing: one line per spoken turn, saved to data/timings.jsonl, summed up by the report."""
import asyncio
import json

import numpy as np

from buddy import timing
from buddy.timing import TurnTimer, report
from tests.test_quiet_wake import setup


def test_stages_add_up(tmp_path):
    t = TurnTimer(speech_end=100.0, endpoint=100.8)
    t.marks.update(stt_done=100.95, tts_sent=102.0, tts_first_byte=102.3, first_audio=102.8)
    t.spans += [("haiku", 101.0, 101.6), ("check", 101.6, 102.0)]
    st = t.stages()
    assert st == {"wait": 800, "ears": 150, "haiku": 600, "check": 400, "reword": None, "search": None,
                  "think": 50, "voice": 300, "hold": 500, "total": 2800, "haiku_calls": 1}
    line = t.line(st)
    assert line.startswith("TURN") and "TOTAL 2800" in line and "fact-check 400" in line
    row = t.finish(tmp_path)
    assert row["total"] == 2800 and t.finish(tmp_path) is None  # only once
    assert json.loads((tmp_path / "timings.jsonl").read_text())["haiku"] == 600


def test_report_gives_middle_average_and_worst(tmp_path):
    with (tmp_path / "timings.jsonl").open("w") as f:
        for total in (1500, 2000, 1700):
            f.write(json.dumps({"total": total, "wait": 800, "ears": 100}) + "\n")
        f.write(json.dumps({"total": None, "ears": 100}) + "\n")  # an unspoken turn is left out
    out = report(tmp_path / "timings.jsonl")
    assert "Last 3 spoken turns" in out
    total_line = next(line for line in out.splitlines() if line.startswith("TOTAL"))
    assert total_line.split()[1:4] == ["1700", "1733", "2000"]


def test_helpers_do_nothing_outside_a_turn():
    timing.mark("stt_done")
    timing.note("x", 1)
    with timing.span("haiku"):
        pass  # no error, nothing recorded


def test_a_spoken_turn_is_timed_end_to_end(settings):
    hub, s, ws, spoken = setup(settings, "what time is it")
    timer = TurnTimer(speech_end=0.0, endpoint=0.0)
    import time
    timer.marks.update(speech_end=time.monotonic() - 0.8, endpoint=time.monotonic())
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0, timer=timer))
    assert spoken  # it answered
    rows = [json.loads(x) for x in (settings.data_dir / "timings.jsonl").read_text().splitlines()]
    assert rows[-1]["text"] == "what time is it" and rows[-1]["wait"] >= 790 and rows[-1]["ears"] is not None
    assert any(e["event"] == "timing" for e in hub.log)


def test_voice_stages_are_measured_when_buddy_speaks(settings):
    import time
    from buddy.hub import Hub, Session
    from tests.fakes import FakeAPIs, FakeSTT

    class FakeTTS:
        last_engine = "fake"

        async def stream(self, text):
            await asyncio.sleep(0.2)  # the voice service thinking
            for _ in range(10):
                yield b"\0" * 3200  # 0.1 s of sound each

    class WS:
        def __init__(self):
            self.session = None

        async def send_text(self, text):
            if json.loads(text).get("type") == "audio_end":
                self.session.playback_done.set()

        async def send_bytes(self, data):
            pass

    apis = FakeAPIs()
    apis.transcripts = ["what time is it"]
    hub = Hub(settings, stt=FakeSTT(apis))
    hub.tts = FakeTTS()
    ws = WS()
    s = Session(hub, ws, "device", "esp32")
    ws.session = s
    hub.sessions = {s.id: s}
    now = time.monotonic()
    timer = TurnTimer(speech_end=now - 0.8, endpoint=now)
    asyncio.run(hub.process_audio(s, np.zeros(32000, np.int16), 1.0, timer=timer))
    row = json.loads((settings.data_dir / "timings.jsonl").read_text().splitlines()[-1])
    assert 180 <= row["voice"] <= 600  # the 0.2 s wait for the first sound
    assert row["hold"] is not None and row["total"] >= row["wait"] + row["voice"]


def test_the_report_opens_in_the_browser(settings):
    from fastapi.testclient import TestClient
    from buddy.main import create_app
    (settings.data_dir / "timings.jsonl").write_text(json.dumps({"total": 1800, "wait": 800}) + "\n")
    with TestClient(create_app(settings=settings)) as c:
        r = c.get("/api/timings")
    assert r.status_code == 200 and "TOTAL" in r.text


def test_speed_tab_gets_the_latest_turns_first(settings):
    from fastapi.testclient import TestClient
    from buddy.main import create_app
    with (settings.data_dir / "timings.jsonl").open("w") as f:
        for n, total in enumerate((2000, 1500, 900)):
            f.write(json.dumps({"turn": n, "text": f"q{n}", "total": total, "marks_ms": {"x": 1}}) + "\n")
        f.write(json.dumps({"turn": 9, "total": None}) + "\n")  # not spoken: left out
    with TestClient(create_app(settings=settings)) as c:
        d = c.get("/api/speed").json()
    assert [t["total"] for t in d["turns"]] == [900, 1500, 2000] and "marks_ms" not in d["turns"][0]
