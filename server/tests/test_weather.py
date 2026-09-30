import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from buddy.weather import Weather, rain_windows
from tests.fakes import make_forecast

LONDON = ZoneInfo("Europe/London")


def test_rain_windows_merge_short_gaps():
    w = rain_windows([13, 14, 15, 16, 17, 18], [10, 60, 20, 70, 80, 10], [0, 0.4, 0, 0.5, 0.9, 0])
    assert len(w) == 1 and w[0].start == 14 and w[0].end == 18 and w[0].peak == 80


def test_spoken_forecast_mentions_rain_timing(settings):
    now = datetime(2026, 9, 30, 9, 30, tzinfo=LONDON)
    data = make_forecast(now, rain={15: 70, 16: 80, 17: 60})
    out = Weather(settings).summarize(data, "today", now)
    s = out["spoken"]
    assert "Right now in Richmond it's 14 degrees" in s
    assert "Today's high is 17 and the low is 9" in s
    assert "Rain is likely from 3pm until about 6pm, up to 80 percent" in s


def test_dry_day(settings):
    now = datetime(2026, 9, 30, 9, 30, tzinfo=LONDON)
    s = Weather(settings).summarize(make_forecast(now), "today", now)["spoken"]
    assert "No rain expected for the rest of the day." in s


def test_tomorrow(settings):
    now = datetime(2026, 9, 30, 9, 30, tzinfo=LONDON)
    s = Weather(settings).summarize(make_forecast(now), "tomorrow", now)["spoken"]
    assert s.startswith("Tomorrow in Richmond: partly cloudy, with a high of 17 and a low of 9.")


def test_fetch_builds_correct_request(settings, apis):
    seen = {}
    orig = apis.handler

    def spy(req):
        seen["url"] = str(req.url)
        return orig(req)
    apis.handler = spy
    w = Weather(settings, apis.client())
    line = asyncio.run(w.context_line())
    assert "latitude=51.4613" in seen["url"] and "precipitation_probability" in seen["url"]
    assert "timezone=Europe%2FLondon" in seen["url"]
    assert line.startswith("Weather in Richmond, London: now 14°C")
