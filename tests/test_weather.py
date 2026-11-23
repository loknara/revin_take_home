import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from revin_take_home.weather import summarize, WeatherWorker

NOW = datetime(2026, 7, 1, 12, tzinfo=timezone.utc)


def feeds(temperature=75):
    return {
        "properties": {
            "updateTime": NOW.isoformat(),
            "periods": [
                {
                    "startTime": (NOW + timedelta(hours=i)).isoformat(),
                    "endTime": (NOW + timedelta(hours=i + 1)).isoformat(),
                    "temperature": temperature,
                    "temperatureUnit": "F",
                }
                for i in range(25)
            ],
        }
    }, {"features": []}


@pytest.mark.parametrize(
    "temperature,hot,cold",
    [(89, False, False), (90, True, False), (40, False, True), (41, False, False)],
)
def test_dispatch_threshold_boundaries(temperature, hot, cold):
    result = summarize("Kings", *feeds(temperature), NOW)
    assert (result["hot"], result["cold"]) == (hot, cold)
    assert result["valid_until"] == (NOW + timedelta(minutes=30)).isoformat()


def test_future_heat_and_celsius_are_included():
    forecast, alerts = feeds()
    forecast["properties"]["periods"][20].update(temperature=35, temperatureUnit="C")
    result = summarize("Kings", forecast, alerts, NOW)
    assert result["hot"] and result["evidence"]["max_f"] == 95


def test_alert_overrides_mild_temperature_and_expired_alert_does_not():
    forecast, alerts = feeds()
    alert = {
        "event": "Extreme Heat Warning",
        "effective": NOW.isoformat(),
        "expires": (NOW + timedelta(hours=3)).isoformat(),
        "status": "Actual",
    }
    alerts["features"] = [{"properties": alert}]
    assert summarize("Kings", forecast, alerts, NOW)["hot"]
    alert["expires"] = (NOW - timedelta(seconds=1)).isoformat()
    assert not summarize("Kings", forecast, alerts, NOW)["hot"]


@pytest.mark.parametrize(
    "problem",
    ["stale", "future", "missing", "gap", "truncated", "nan", "unit", "alert"],
)
def test_bad_data_is_not_mild_weather(problem):
    forecast, alerts = feeds()
    p = forecast["properties"]
    if problem == "stale":
        p["updateTime"] = (NOW - timedelta(hours=7)).isoformat()
    if problem == "future":
        p["updateTime"] = (NOW + timedelta(hours=1)).isoformat()
    if problem == "missing":
        p["periods"][0]["temperature"] = None
    if problem == "gap":
        p["periods"].pop(3)
    if problem == "truncated":
        p["periods"] = p["periods"][:20]
    if problem == "nan":
        p["periods"][0]["temperature"] = float("nan")
    if problem == "unit":
        p["periods"][0]["temperatureUnit"] = "K"
    if problem == "alert":
        alerts = {}
    with pytest.raises((ValueError, KeyError)):
        summarize("Kings", forecast, alerts, NOW)


def test_failed_fetch_cannot_extend_cache_validity():
    async def run():
        writes = []

        async def weather(req):
            return httpx.Response(503)

        async def database(req):
            writes.append(req)
            return httpx.Response(201)

        async with (
            httpx.AsyncClient(transport=httpx.MockTransport(weather)) as nws,
            httpx.AsyncClient(
                base_url="https://database.test/",
                transport=httpx.MockTransport(database),
            ) as db,
        ):
            result = await WeatherWorker(db, nws).refresh_county("Kings", (40.6, -73.9))
        assert result is False and writes == []

    asyncio.run(run())
