"""Timestamped field evidence shared by personalized ranking and replay."""
from __future__ import annotations

from datetime import timedelta, timezone
from math import isfinite

from pyproj import Geod

from app.adapters.satellite import filter_observations
from app.adapters.weather import available_records
from app.domain import FieldSignals, PersonalizationPolicy, as_of_cutoff


def build_index_signals(entries, as_of, policy: PersonalizationPolicy, signals: FieldSignals):
    """Use independent NDVI/NDMI series; never interpret NDMI as soil moisture."""
    grouped = {}
    for series, source in entries:
        payload = series.model_dump(mode="python")
        visible = filter_observations(payload, as_of)
        if visible["temporal_rejected_count"]:
            signals.issues.append(f"{series.index}_TEMPORAL_REJECTED")
        for row in visible["observations"]:
            value = row.get("mean")
            if value is None or not isfinite(value) or not -1 <= value <= 1:
                signals.issues.append(f"{series.index}_INVALID_MEAN")
                continue
            if row["valid_pixel_fraction"] < policy.min_valid_fraction:
                signals.issues.append(f"{series.index}_LOW_COVERAGE")
                continue
            statistics = [row.get(name) for name in ("median", "p10", "p90")]
            if any(v is not None and (not isfinite(v) or not -1 <= v <= 1) for v in statistics):
                signals.issues.append(f"{series.index}_INVALID_STATISTICS")
                continue
            median, p10, p90 = statistics
            if ((p10 is not None and p90 is not None and p10 > p90) or
                    (median is not None and ((p10 is not None and median < p10) or
                                            (p90 is not None and median > p90)))):
                signals.issues.append(f"{series.index}_INVALID_STATISTICS")
                continue
            grouped.setdefault(series.index, []).append((row, source, series))
    for index, observations in grouped.items():
        # A revised observation replaces that acquisition, never counts as a trend.
        by_day = {}
        for row, source, series in sorted(observations, key=lambda e: (
                e[0]["date"], not e[1].startswith("image:"), e[0]["available_at"], e[1])):
            by_day[row["date"]] = (row, source, series)
        recent = sorted(by_day.values(), key=lambda e: e[0]["date"])
        row, source, series = recent[-1]
        age = (as_of - row["date"]).days
        summary = {"date": row["date"].isoformat(), "mean": row["mean"],
                   "valid_pixel_fraction": row["valid_pixel_fraction"], "age_days": age,
                   "source": source, "sensor": series.sensor,
                   "processing_version": series.processing_version,
                   "native_resolution_m": series.native_resolution_m,
                   "observation_count": len(recent)}
        signals.indices[index] = summary
        if age > policy.max_index_age_days:
            signals.issues.append(f"{index}_STALE")
            continue
        metric = f"{index}.mean"
        signals.metrics[metric] = row["mean"]
        signals.metric_dates[metric] = row["date"]
        signals.metric_sources[metric] = [source]
        signals.source_availability[source] = row["available_at"]
        compatible = [entry for entry in recent[:-1]
                      if entry[2].sensor == series.sensor
                      and entry[2].processing_version == series.processing_version
                      and entry[2].native_resolution_m == series.native_resolution_m
                      and (row["date"] - entry[0]["date"]).days <= policy.max_index_age_days]
        if compatible:
            previous, previous_source, _ = compatible[-1]
            metric = f"{index}.delta"
            signals.metrics[metric] = row["mean"] - previous["mean"]
            signals.metric_dates[metric] = row["date"]
            signals.metric_sources[metric] = [previous_source, source]
            signals.source_availability[previous_source] = previous["available_at"]


def build_local_weather(data, geometry, as_of, policy, signals, source):
    """Keep one point/provider, complete daily windows and forecast issuance."""
    point = geometry.geometry.centroid
    _, _, distance = Geod(ellps="WGS84").inv(point.x, point.y, data.longitude, data.latitude)
    if distance > data.max_distance_km * 1000:
        raise ValueError("WEATHER_TOO_FAR_FROM_FIELD")
    records = available_records(data.records, as_of)
    records = [r.model_copy(update={"valid_time": r.valid_time.astimezone(timezone.utc),
        "available_at": r.available_at.astimezone(timezone.utc),
        "issued_at": r.issued_at.astimezone(timezone.utc) if r.issued_at else None}) for r in records]
    if records:
        signals.source_availability[source] = max(r.available_at for r in records)
    if len(records) != len(data.records):
        signals.issues.append("LOCAL_WEATHER_TEMPORAL_REJECTED")
    limit = as_of_cutoff(as_of)
    valid = [r for r in records if r.kind in {"observation", "reanalysis"}
             and r.valid_time.date() < as_of
             and 0 <= (as_of - r.valid_time.date()).days <= policy.max_weather_age_days]
    signals.weather = {"latitude": data.latitude, "longitude": data.longitude,
                       "distance_km": distance / 1000, "resolution_m": data.resolution_m,
                       "source": source, "records_available": len(records)}
    if not valid:
        signals.issues.append("LOCAL_WEATHER_MISSING_OR_STALE")
    else:
        # Prefer nearby measured observations to reanalysis; never splice baselines.
        chosen = max(valid, key=lambda r: (r.kind == "observation", r.valid_time, r.available_at))
        provider, kind = chosen.provider, chosen.kind
        days = {}
        for r in sorted(records, key=lambda r: r.available_at):
            if r.provider == provider and r.kind == kind and r.valid_time.date() < as_of:
                days[r.valid_time.date()] = r.values
        end = chosen.valid_time.date()
        signals.weather.update(provider=provider, kind=kind, state_date=end.isoformat())
        for metric, variable, count, operation in (
            ("precip_7d", "precip", 7, sum), ("precip_30d", "precip", 30, sum),
            ("tmin_7d", "tmin", 7, min), ("tmax_7d", "tmax", 7, max),
            ("soil_moisture", "soil_moisture_0_7", 1, min),
        ):
            values = [days.get(end - timedelta(days=i), {}).get(variable) for i in range(count)]
            if all(_physical(variable, value) for value in values):
                _metric(signals, f"weather.{metric}", operation(values), end, source)
        precip = [days.get(end - timedelta(days=i), {}).get("precip") for i in range(30)]
        et0 = [days.get(end - timedelta(days=i), {}).get("et0") for i in range(30)]
        if all(_physical("precip", p) and _physical("et0", e) for p, e in zip(precip, et0)):
            _metric(signals, "weather.balance_30d", sum(precip) - sum(et0), end, source)
    forecasts = [r for r in records if r.kind == "forecast" and r.valid_time > limit
                 and r.valid_time.date() <= as_of + timedelta(days=7)]
    if forecasts:
        # Only a complete single issuance is used; no mixing forecasts from runs.
        selected = max(forecasts, key=lambda r: (r.issued_at, r.provider))
        run = {r.valid_time.date(): r.values for r in forecasts
               if (r.issued_at, r.provider) == (selected.issued_at, selected.provider)}
        for metric, variable, operation in (("tmin_7d", "tmin", min),
                                             ("tmax_7d", "tmax", max),
                                             ("precip_7d", "precip", sum)):
            values = [run.get(as_of + timedelta(days=i), {}).get(variable) for i in range(1, 8)]
            if all(_physical(variable, value) for value in values):
                _metric(signals, f"forecast.{metric}", operation(values), as_of, source)
        signals.weather.update(forecast_provider=selected.provider,
                               forecast_issued_at=selected.issued_at.isoformat())


def _physical(variable, value):
    if value is None or not isfinite(value):
        return False
    if variable in {"precip", "et0"}:
        return value >= 0
    if variable.startswith("soil_moisture"):
        return 0 <= value <= 1
    return -100 <= value <= 70


def _metric(signals, name, value, day, source):
    signals.metrics[name] = value
    signals.metric_dates[name] = day
    signals.metric_sources[name] = [source]
