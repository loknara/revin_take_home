"""Refresh public county forecasts off the call path; SQL makes the priority decision.

NWS API: https://www.weather.gov/documentation/services-web-api
County representative points are approximate, not indoor-temperature measurements.
"""
import asyncio
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import logging
import math
import os

import httpx

log = logging.getLogger(__name__)
COUNTY_POINTS = {'New York': (40.7831, -73.9712), 'Kings': (40.6782, -73.9442),
                 'Queens': (40.7282, -73.7949)}
HEAT_ALERTS = {'Heat Advisory', 'Excessive Heat Warning', 'Excessive Heat Watch',
               'Extreme Heat Warning', 'Extreme Heat Watch'}
COLD_ALERTS = {'Cold Weather Advisory', 'Extreme Cold Warning', 'Extreme Cold Watch',
               'Wind Chill Advisory', 'Wind Chill Warning', 'Wind Chill Watch',
               'Freeze Warning', 'Freeze Watch', 'Frost Advisory'}


def timestamp(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Weather timestamp has no timezone')
    return parsed


def summarize(county, forecast, alerts, now, hot_f=90.0, cold_f=40.0):
    """Reject stale, incomplete or malformed feeds instead of treating them as mild."""
    properties = forecast['properties']
    updated = timestamp(properties['updateTime'])
    if updated > now + timedelta(minutes=5) or now - updated > timedelta(hours=6):
        raise ValueError('Forecast is stale')
    end = now + timedelta(hours=24)
    periods = sorted(properties['periods'], key=lambda p: timestamp(p['startTime']))
    temperatures = []
    covered_until = now
    for period in periods:
        start, stop = timestamp(period['startTime']), timestamp(period['endTime'])
        if stop <= now or start >= end:
            continue
        if start > covered_until or stop <= start:
            raise ValueError('Forecast coverage gap')
        value = period['temperature']
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
            raise ValueError('Missing temperature')
        unit = period['temperatureUnit']
        if unit == 'C': value = value * 9 / 5 + 32
        elif unit != 'F': raise ValueError('Unknown temperature unit')
        temperatures.append(value)
        covered_until = max(covered_until, stop)
    if not temperatures or covered_until < end:
        raise ValueError('Incomplete 24-hour forecast')
    events = []
    for item in alerts['features']:
        alert = item['properties']
        start = timestamp(alert.get('onset') or alert['effective'])
        stop = timestamp(alert.get('ends') or alert['expires'])
        if stop > now and start < end and alert.get('status', 'Actual') == 'Actual':
            events.append(alert['event'])
    return {
        'county': county, 'fetched_at': now.isoformat(),
        'valid_until': (now + timedelta(minutes=30)).isoformat(),
        'hot': max(temperatures) >= hot_f or bool(set(events) & HEAT_ALERTS),
        'cold': min(temperatures) <= cold_f or bool(set(events) & COLD_ALERTS),
        'evidence': {'source': 'NWS', 'forecast_updated_at': updated.isoformat(),
                     'horizon_start': now.isoformat(), 'horizon_end': end.isoformat(),
                     'min_f': min(temperatures), 'max_f': max(temperatures),
                     'alerts': sorted(set(events)), 'hot_threshold_f': hot_f,
                     'cold_threshold_f': cold_f, 'location_precision': 'county_representative_point'},
    }


class WeatherWorker:
    def __init__(self, database, client):
        self.database = database
        self.client = client
        self.hourly_urls = {}
        self.hot_f = float(os.getenv('WEATHER_HOT_F', '90'))
        self.cold_f = float(os.getenv('WEATHER_COLD_F', '40'))
        if not (math.isfinite(self.hot_f) and math.isfinite(self.cold_f) and self.cold_f < self.hot_f):
            raise ValueError('Invalid weather dispatch thresholds')

    async def get(self, url, **kwargs):
        response = await self.client.get(url, **kwargs)
        response.raise_for_status()
        return response.json()

    async def refresh_county(self, county, point):
        coordinates = ','.join(str(v) for v in point)
        try:
            async with asyncio.timeout(15):
                if county not in self.hourly_urls:
                    metadata = await self.get('https://api.weather.gov/points/' + coordinates)
                    url = metadata['properties']['forecastHourly']
                    if not url.startswith('https://api.weather.gov/gridpoints/'):
                        raise ValueError('Unexpected forecast URL')
                    self.hourly_urls[county] = url
                forecast, alerts = await asyncio.gather(
                    self.get(self.hourly_urls[county]),
                    self.get('https://api.weather.gov/alerts/active', params={'point': coordinates}),
                )
                snapshot = summarize(county, forecast, alerts, datetime.now(timezone.utc), self.hot_f, self.cold_f)
                response = await self.database.post('service_weather', params={'on_conflict': 'county'},
                    headers={'Prefer': 'resolution=merge-duplicates'}, json=snapshot, timeout=3)
                response.raise_for_status()
                return True
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            # Do not extend the old record's validity after a failed refresh.
            self.hourly_urls.pop(county, None)
            log.warning('Weather refresh unavailable for %s; old data expires normally', county)
            return False

    async def refresh(self):
        return await asyncio.gather(*(self.refresh_county(c, p) for c, p in COUNTY_POINTS.items()))

    async def run(self):
        while True:
            await self.refresh()
            await asyncio.sleep(15 * 60)


async def stop_worker(task):
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
