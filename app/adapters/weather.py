"""Timestamped weather sources. Importing this module never starts network I/O."""
from __future__ import annotations

import json
import time
from datetime import date, datetime, time as day_time, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.domain import WeatherRecord, as_of_cutoff

WeatherKind = Literal["reanalysis", "forecast", "observation", "climatology"]
Period = tuple[date, date]
DAILY_VARIABLES = {"tmin": "temperature_2m_min", "tmax": "temperature_2m_max",
                   "precip": "precipitation_sum", "et0": "et0_fao_evapotranspiration"}
HOURLY_VARIABLES = {"soil_moisture_0_7": "soil_moisture_0_to_7cm",
                    "soil_moisture_7_28": "soil_moisture_7_to_28cm", "snow_depth": "snow_depth"}


def cutoff(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("as_of must include a timezone")
        return value.astimezone(timezone.utc)
    return as_of_cutoff(value)


def available_records(records: list[WeatherRecord], as_of: date | datetime) -> list[WeatherRecord]:
    """Never substitute valid time for a record's actual availability or issuance."""
    limit = cutoff(as_of)
    result = []
    for record in records:
        if record.available_at > limit:
            continue
        if record.kind == "forecast":
            if (record.issued_at is None or record.issued_at > limit
                    or record.issued_at > record.available_at
                    or record.valid_time < record.issued_at
                    or record.valid_time > record.issued_at + timedelta(days=16)):
                continue
        elif record.valid_time > limit or record.available_at < record.valid_time:
            continue
        result.append(record.model_copy(deep=True))
    return sorted(result, key=lambda r: (r.valid_time, r.provider, r.kind, r.available_at))


class WeatherProvider(Protocol):
    def get(self, kind: WeatherKind, lat: float, lon: float, vars: list[str],
            period: Period, as_of: date | datetime) -> list[WeatherRecord]: ...


class WeatherFile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    records: list[WeatherRecord]


class FileWeatherProvider:
    """A local point export with explicit timestamps, including archived forecasts."""
    def __init__(self, path: str | Path):
        self.data = WeatherFile.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))

    def records_for(self, lat: float, lon: float, as_of: date | datetime) -> list[WeatherRecord]:
        if abs(lat - self.data.latitude) > 1e-6 or abs(lon - self.data.longitude) > 1e-6:
            raise ValueError("WEATHER_LOCATION_MISMATCH: file coordinates differ from field point")
        return available_records(self.data.records, as_of)

    def get(self, kind: WeatherKind, lat: float, lon: float, vars: list[str],
            period: Period, as_of: date | datetime) -> list[WeatherRecord]:
        _validate_query(kind, lat, lon, vars, period)
        return [r.model_copy(update={"values": {v: r.values.get(v) for v in vars}}, deep=True)
                for r in self.records_for(lat, lon, as_of)
                if r.kind == kind and period[0] <= r.valid_time.date() <= period[1]]


def _validate_query(kind, lat, lon, variables, period):
    if kind not in {"forecast", "reanalysis", "climatology", "observation"}:
        raise ValueError("Unknown weather kind")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("Invalid WGS84 weather point")
    if not variables or set(variables) - (DAILY_VARIABLES.keys() | HOURLY_VARIABLES.keys()):
        raise ValueError("Unsupported or empty weather variables")
    if period[0] > period[1]:
        raise ValueError("Weather period is reversed")


class OpenMeteoProvider:
    """Opt-in prototype source; newly fetched data is known only at retrieval time.

    The ordinary API does not supply model issuance time. A forecast therefore
    requires independently attested ``forecast_issued_at``; no issuance is guessed.
    Use timestamped file exports for historical replay.
    """
    ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
    FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, *, enabled: bool = False, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 sleep: Callable[[float], None] = time.sleep,
                 forecast_issued_at: datetime | None = None):
        self.enabled, self.transport, self.clock, self.sleep = enabled, transport, clock, sleep
        self.forecast_issued_at = forecast_issued_at
        self._cache: dict[tuple, list[WeatherRecord]] = {}

    def get(self, kind: WeatherKind, lat: float, lon: float, vars: list[str],
            period: Period, as_of: date | datetime) -> list[WeatherRecord]:
        _validate_query(kind, lat, lon, vars, period)
        if not self.enabled:
            raise ValueError("WEATHER_NETWORK_DISABLED")
        if kind == "observation":
            raise ValueError("Open-Meteo reanalysis is not a station observation")
        limit, started = cutoff(as_of), cutoff(self.clock())
        # A current API response cannot establish knowledge at an earlier cutoff.
        if started > limit:
            return []
        issued = self.forecast_issued_at if kind == "forecast" else None
        if kind == "forecast":
            if issued is None:
                raise ValueError("FORECAST_ISSUED_AT_UNAVAILABLE: use a timestamped export")
            issued = cutoff(issued)
            if issued > started or issued > limit:
                return []
        key = (kind, lat, lon, tuple(sorted(vars)), period, limit, issued)
        if key in self._cache:
            return [r.model_copy(deep=True) for r in self._cache[key]]
        params = {"latitude": lat, "longitude": lon, "timezone": "UTC",
                  "start_date": period[0].isoformat(), "end_date": period[1].isoformat()}
        if kind == "forecast":
            params["forecast_days"] = 16
            url = self.FORECAST_URL
        else:
            # Pin one dataset; never compare best-match mixtures across decades.
            params["models"] = "era5_land"
            url = self.ARCHIVE_URL
        daily = {v: DAILY_VARIABLES[v] for v in vars if v in DAILY_VARIABLES}
        hourly = {v: HOURLY_VARIABLES[v] for v in vars if v in HOURLY_VARIABLES}
        if daily:
            params["daily"] = ",".join(daily.values())
        if hourly:
            params["hourly"] = ",".join(hourly.values())
        payload = self._fetch(url, params)
        retrieved = cutoff(self.clock())
        if retrieved > limit:
            return []
        values_by_day: dict[date, dict[str, float | None]] = {}
        for variable, remote in daily.items():
            for timestamp, value in _column(payload, "daily", remote):
                values_by_day.setdefault(date.fromisoformat(timestamp), {})[variable] = value
        for variable, remote in hourly.items():
            buckets: dict[date, list[tuple[str, float | None]]] = {}
            for timestamp, value in _column(payload, "hourly", remote):
                buckets.setdefault(datetime.fromisoformat(timestamp).date(), []).append((timestamp, value))
            for day, samples in buckets.items():
                # Daily means require all 24 distinct UTC hours and finite values.
                vals = [v for _, v in samples]
                complete = (len({t for t, _ in samples}) == 24 and len(vals) == 24
                            and all(v is not None for v in vals))
                value = sum(vals) / 24 if complete else None
                values_by_day.setdefault(day, {})[variable] = value
        records = []
        for day, values in values_by_day.items():
            if not period[0] <= day <= period[1]:
                continue
            valid = datetime.combine(day, day_time.min, timezone.utc)
            if kind != "forecast" and as_of_cutoff(day) > retrieved:
                continue  # Incomplete current-day totals are not daily observations.
            records.append(WeatherRecord(valid_time=valid, available_at=retrieved,
                issued_at=issued, kind=kind, provider="open-meteo:" + (
                    "operational-forecast" if kind == "forecast" else "era5_land"),
                values={v: values.get(v) for v in vars}))
        result = available_records(records, limit)
        self._cache[key] = result
        return [r.model_copy(deep=True) for r in result]

    def _fetch(self, url, params):
        with httpx.Client(transport=self.transport, timeout=20) as client:
            for attempt in range(3):
                try:
                    response = client.get(url, params=params)
                    if response.status_code == 429 or response.status_code >= 500:
                        response.raise_for_status()
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict) or payload.get("error"):
                        raise ValueError("Invalid weather provider response")
                    return payload
                except (httpx.TimeoutException, httpx.TransportError, httpx.HTTPStatusError) as exc:
                    transient = not isinstance(exc, httpx.HTTPStatusError) or (
                        exc.response.status_code == 429 or exc.response.status_code >= 500)
                    if not transient or attempt == 2:
                        raise
                    self.sleep(.25 * 2 ** attempt)
        raise ValueError("Weather provider returned no response")


def _column(payload, section, name):
    block = payload.get(section, {})
    timestamps, values = block.get("time", []), block.get(name)
    if values is None:
        return []
    if len(timestamps) != len(values):
        raise ValueError("Weather response column length mismatch")
    return zip(timestamps, values, strict=True)
