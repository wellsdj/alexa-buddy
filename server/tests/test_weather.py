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


# ---- past week, clouds, wind only when asked ----
from datetime import timedelta  # noqa: E402

from buddy.weather import day_indices, split_past  # noqa: E402


def _with_past(now, days_back=7):
    """A forecast shaped like Open-Meteo's with past_days=7: the past days come first."""
    data = make_forecast(now - timedelta(days=days_back), days=7 + days_back)
    n = len(data["daily"]["time"])
    data["daily"]["precipitation_sum"] = [3.2 if i == days_back - 1 else 0.0 for i in range(n)]
    data["daily"]["cloud_cover_mean"] = [85] * n
    data["daily"]["wind_speed_10m_max"] = [18.0] * n
    data["daily"]["wind_gusts_10m_max"] = [34.0] * n
    data["current"].update(cloud_cover=92, wind_speed_10m=11.0, wind_gusts_10m=25.0)
    return split_past(data, now.date().isoformat())


def test_fetch_asks_for_past_week_and_clouds(settings, apis):
    seen = {}
    orig = apis.handler

    def spy(req):
        seen["url"] = str(req.url)
        return orig(req)
    apis.handler = spy
    asyncio.run(Weather(settings, apis.client()).fetch())
    assert "past_days=7" in seen["url"] and "cloud_cover" in seen["url"] and "wind_gusts_10m" in seen["url"]


def test_split_past_makes_today_index_zero():
    now = datetime(2026, 11, 27, 9, tzinfo=LONDON)
    data = _with_past(now)
    assert data["daily"]["time"][0] == "2026-11-27"
    assert data["past"]["time"][-1] == "2026-11-26" and len(data["past"]["time"]) == 7
    assert data["hourly"]["time"][0].startswith("2026-11-27")


def test_past_phrases():
    now = datetime(2026, 11, 27, 9, tzinfo=LONDON)  # a Friday
    assert day_indices("what was the weather yesterday", now) == [-1]
    assert day_indices("has it rained this past week", now) == list(range(-7, 0))
    assert day_indices("what was it like on monday", now) == [-4]
    assert day_indices("weather last friday", now) == [-7]
    assert day_indices("3 days ago", now) == [-3]
    assert day_indices("what was the weather last month", now) == []
    assert day_indices("weather on monday", now) == [3]  # no past tense: the coming Monday


def test_yesterday_report_says_how_much_rain(settings):
    now = datetime.now(LONDON)
    s = Weather(settings).past_report(_with_past(now), [-1], "Richmond")
    assert s.startswith("In Richmond, yesterday was") and "3.2 millimetres of rain" in s


def test_past_week_counts_wet_days(settings):
    now = datetime.now(LONDON)
    s = Weather(settings).past_report(_with_past(now), list(range(-7, 0)), "Richmond")
    assert "over the last 7 days it rained on 1 of them" in s


def test_wind_only_when_asked(settings):
    now = datetime(2026, 11, 27, 9, 30, tzinfo=LONDON)
    data = _with_past(now)
    w = Weather(settings)
    plain = w.summarize(data, now=now, idx=0, asked="what's the weather")["spoken"]
    assert "wind" not in plain.lower()
    windy = w.summarize(data, now=now, idx=0, asked="is it windy")["spoken"]
    assert "Wind is 11 miles an hour, a light breeze, with gusts to 25." in windy
    tomorrow = w.summarize(data, now=now, idx=1, asked="how windy tomorrow")["spoken"]
    assert "Wind up to 18 miles an hour, breezy, with gusts to 34." in tomorrow


def test_clouds_when_asked(settings):
    now = datetime(2026, 11, 27, 9, 30, tzinfo=LONDON)
    s = Weather(settings).summarize(_with_past(now), now=now, idx=0, asked="is it cloudy")["spoken"]
    assert "Cloud cover is about 92 percent, so overcast." in s


def test_windy_and_cloudy_are_weather_questions():
    from buddy.intents import match
    now = datetime(2026, 11, 27, 9, tzinfo=LONDON)
    for q in ("is it windy outside", "how cloudy is it tomorrow", "what was the weather yesterday"):
        assert match(q, now).name == "weather", q
