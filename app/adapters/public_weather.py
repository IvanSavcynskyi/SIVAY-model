"""Coordinate-based public weather collection with pinned forecast runs."""
from datetime import datetime, timedelta, timezone
import logging
import time

import httpx

from app.domain import FieldWeatherInput, WeatherRecord

DAILY = {"tmin": "temperature_2m_min", "tmax": "temperature_2m_max",
         "precip": "precipitation_sum", "et0": "et0_fao_evapotranspiration"}
logger = logging.getLogger(__name__)


def fetch_json(method, url, *, transport=None, params=None, payload=None):
    with httpx.Client(transport=transport, timeout=15, follow_redirects=False) as client:
        for attempt in range(3):
            try:
                response = client.request(method, url, params=params, json=payload)
                response.raise_for_status()
                if len(response.content) > 4 * 1024 * 1024:
                    raise ValueError("PUBLIC_RESPONSE_TOO_LARGE")
                data = response.json()
                if not isinstance(data, dict) or data.get("error"):
                    raise ValueError("INVALID_PUBLIC_RESPONSE")
                return data
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                retry = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code in {429, 500, 502, 503, 504}
                if not retry or attempt == 2:
                    raise
                time.sleep(.25 * 2 ** attempt)


class PublicWeather:
    def __init__(self, transport=None, clock=lambda: datetime.now(timezone.utc)):
        self.transport, self.clock = transport, clock

    def collect(self, geometry):
        now = self.clock().astimezone(timezone.utc)
        point = geometry.geometry.centroid
        common = {"latitude": point.y, "longitude": point.x, "timezone": "UTC",
                  "temperature_unit": "celsius", "precipitation_unit": "mm",
                  "daily": ",".join(DAILY.values())}
        # IFS analysis has a shorter publication delay than ERA5-Land. Keep one
        # model for all history windows, with actual retrieval-time availability.
        history = fetch_json("GET", "https://archive-api.open-meteo.com/v1/archive", transport=self.transport,
            params=common | {"start_date": (now.date()-timedelta(days=35)).isoformat(),
                "end_date": (now.date()-timedelta(days=2)).isoformat(), "models": "ecmwf_ifs",
                "hourly": "soil_moisture_0_to_7cm"})
        retrieved = self.clock().astimezone(timezone.utc)
        records = self._records(history, "reanalysis", "open-meteo:ecmwf_ifs", retrieved)
        # A model run is selected explicitly. Its initialization and the observed
        # time of public access remain distinct; no past availability is guessed.
        cycle = (now - timedelta(hours=6)).replace(hour=((now-timedelta(hours=6)).hour//12)*12,
                                                   minute=0, second=0, microsecond=0)
        try:
            forecast = fetch_json("GET", "https://single-runs-api.open-meteo.com/v1/forecast",
                transport=self.transport, params=common | {"models": "ecmwf_ifs",
                    "run": cycle.strftime("%Y-%m-%dT%H:%M"),
                    "forecast_days": 10})
            records += self._records(forecast, "forecast", "open-meteo:ecmwf_ifs:single-run",
                                     self.clock().astimezone(timezone.utc), cycle)
            forecast_error = None
        except (httpx.HTTPError, ValueError) as exc:
            detail = exc.response.text[:500] if isinstance(exc, httpx.HTTPStatusError) else str(exc)
            logger.warning("Pinned weather forecast unavailable: %s", detail)
            # Historical observations remain useful if a forecast run is absent.
            forecast_error = "PUBLIC_FORECAST_UNAVAILABLE"
        data = FieldWeatherInput(latitude=point.y, longitude=point.x, resolution_m=9000, records=records)
        return {"weather": data.model_dump(mode="json"), "forecast_error": forecast_error,
                "provider": "Open-Meteo / ECMWF IFS", "model_run": cycle.isoformat(),
                "grid_latitude": history.get("latitude"), "grid_longitude": history.get("longitude"),
                "retrieved_at": self.clock().astimezone(timezone.utc).isoformat()}

    @staticmethod
    def _records(payload, kind, provider, retrieved, issued=None):
        daily = payload.get("daily", {})
        times = daily.get("time", [])
        values = {name: daily.get(remote, [None]*len(times)) for name, remote in DAILY.items()}
        if any(len(column) != len(times) for column in values.values()):
            raise ValueError("WEATHER_COLUMN_LENGTH_MISMATCH")
        hourly = payload.get("hourly", {})
        soil = {}
        soil_values, soil_times = hourly.get("soil_moisture_0_to_7cm", []), hourly.get("time", [])
        if len(soil_values) != len(soil_times):
            raise ValueError("WEATHER_HOURLY_LENGTH_MISMATCH")
        for stamp, value in zip(soil_times, soil_values, strict=True):
            soil.setdefault(stamp[:10], {})[stamp] = value
        records = []
        for position, day in enumerate(times):
            valid = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            if kind != "forecast" and valid.date() >= retrieved.date():
                continue
            measurements = {name: column[position] for name, column in values.items()}
            samples = list(soil.get(day, {}).values())
            if len(samples) == 24 and all(v is not None for v in samples):
                measurements["soil_moisture_0_7"] = sum(samples)/24
            if any(v is not None for v in measurements.values()):
                records.append(WeatherRecord(valid_time=valid, available_at=retrieved,
                    issued_at=issued, kind=kind, provider=provider, values=measurements))
        return records
