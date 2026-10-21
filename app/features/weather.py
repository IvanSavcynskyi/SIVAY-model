"""As-of weather anomalies; agronomic classifications require reviewed parameters."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from math import isfinite

from app.adapters.weather import available_records, cutoff
from app.domain import RankingParameters, RiskIndicator, WeatherFeatures, WeatherRecord

SOIL_VARIABLE = "soil_moisture_0_7"
BASELINE_YEARS = range(1991, 2021)


def empirical_percentile(value: float, reference: list[float]) -> float | None:
    """Midrank empirical CDF; repeated values are centered, never ranked arbitrarily."""
    if not reference or not isfinite(value) or not all(isfinite(v) for v in reference):
        return None
    return 100 * (sum(v < value for v in reference) + .5 * sum(v == value for v in reference)) / len(reference)


def build_weather_features(records: list[WeatherRecord], as_of: date | datetime,
                           parameters: RankingParameters | None = None, *,
                           max_current_age_days: int = 7, precipitation_days: int = 30) -> WeatherFeatures:
    """Compare one provider's current daily state with complete 1991–2020 windows.

    Freshness (7 days) is an operational input limit, not an agronomic threshold.
    All 30 annual samples must be present and already available. No interpolation,
    provider mixing, fabricated data, or unreviewed multipliers are permitted.
    """
    if max_current_age_days < 0 or precipitation_days < 1:
        raise ValueError("Invalid weather freshness or accumulation window")
    limit = cutoff(as_of)
    visible = available_records(records, limit)
    issues = []
    if len(visible) != len(records):
        issues.append("WEATHER_UNAVAILABLE_RECORDS_FILTERED")
    current = [r for r in visible if r.kind in {"observation", "reanalysis"}
               and timedelta(0) <= limit.date() - r.valid_time.date() <= timedelta(days=max_current_age_days)
               and any(r.values.get(v) is not None for v in (SOIL_VARIABLE, "precip"))]
    if not current:
        return WeatherFeatures(issues=issues + ["WEATHER_CURRENT_MISSING_OR_STALE"])
    latest = max(r.valid_time for r in current)
    chosen = [r for r in current if r.valid_time == latest]
    providers = {r.provider for r in chosen}
    if len(providers) != 1:
        return WeatherFeatures(issues=issues + ["WEATHER_CURRENT_PROVIDER_AMBIGUOUS"])
    provider = next(iter(providers))
    target_date = latest.date()
    source = [r for r in visible if r.provider == provider and r.kind != "forecast"]
    # Multiple revisions are resolved using the latest revision available by as_of.
    days: dict[date, dict[str, float | None]] = {}
    used = []
    for r in sorted(source, key=lambda r: (r.available_at, r.valid_time, r.kind)):
        values = days.setdefault(r.valid_time.date(), {})
        for variable, value in r.values.items():
            values[variable] = value
    current_values = days[target_date]
    soil = current_values.get(SOIL_VARIABLE)
    precipitation = _sum_window(days, target_date, precipitation_days)
    soil_reference, precipitation_reference = [], []
    for year in BASELINE_YEARS:
        try:
            day = target_date.replace(year=year)
        except ValueError:
            continue  # Feb 29 has no 30-year same-day sample; report missing.
        sample = days.get(day, {}).get(SOIL_VARIABLE)
        if sample is not None:
            soil_reference.append(sample)
        sample_precip = _sum_window(days, day, precipitation_days)
        if sample_precip is not None:
            precipitation_reference.append(sample_precip)
    soil_percentile = empirical_percentile(soil, soil_reference) if (
        soil is not None and len(soil_reference) == 30) else None
    precipitation_percentile = empirical_percentile(precipitation, precipitation_reference) if (
        precipitation is not None and len(precipitation_reference) == 30) else None
    if soil_percentile is None:
        issues.append("SOIL_BASELINE_OR_CURRENT_INCOMPLETE")
    if precipitation_percentile is None:
        issues.append("PRECIPITATION_BASELINE_OR_CURRENT_INCOMPLETE")
    dry = getattr(parameters, "weather_dry_percentile", None)
    wet = getattr(parameters, "weather_wet_percentile", None)
    approved = bool(parameters and parameters.status == "approved"
                    and parameters.source.verified_by and parameters.source.verified_at
                    and (parameters.source.ref or parameters.source.url))
    signal, multiplier = "unknown", None
    if not approved or dry is None or wet is None:
        issues.append("UNAPPROVED_WEATHER_THRESHOLDS_DISABLED")
    elif not 0 <= dry < wet <= 100:
        raise ValueError("Weather percentile thresholds must satisfy 0 <= dry < wet <= 100")
    elif soil_percentile is not None and precipitation_percentile is not None:
        signal = ("dry" if soil_percentile < dry and precipitation_percentile < dry
                  else "wet_ok" if soil_percentile > wet else "normal")
        multiplier = parameters.weather_multipliers.get(signal)
        if multiplier is None:
            issues.append("APPROVED_WEATHER_MULTIPLIER_MISSING")
    # Unknown categories are explicit until crop/region hazard thresholds are reviewed.
    risks = [RiskIndicator(code=code, level="unknown")
             for code in ("FROST", "WINTERKILL", "HEAT", "DRY_SPELL")]
    issues.append("CLIMATOLOGICAL_RISK_THRESHOLDS_NOT_CONFIGURED")
    for r in source:
        if r.valid_time.date() == target_date or 1990 <= r.valid_time.year <= 2020 or (
                target_date - timedelta(days=precipitation_days - 1) <= r.valid_time.date() <= target_date):
            used.append(r)
    forecast = [r for r in visible if r.kind == "forecast" and r.valid_time > limit]
    if not forecast:
        issues.append("WEATHER_FORECAST_MISSING")
    provenance = [{"provider": provider, "kind": "daily_anomaly",
                   "baseline": "1991-2020", "baseline_soil_years": len(soil_reference),
                   "baseline_precipitation_years": len(precipitation_reference),
                   "soil_variable": SOIL_VARIABLE, "precipitation_days": precipitation_days,
                   "state_date": target_date.isoformat(),
                   "state_age_days": (limit.date() - target_date).days,
                   "percentile_method": "empirical_midrank", "records_used": len(used),
                   "forecast_records_available": len(forecast),
                   "forecast_used_for_anomaly": False}]
    complete = soil_percentile is not None and precipitation_percentile is not None
    return WeatherFeatures(available_at=max(r.available_at for r in used), signal=signal,
        moisture_percentile=soil_percentile, precipitation_percentile=precipitation_percentile,
        multiplier=multiplier, quality="medium" if complete else "low", risks=risks,
        provenance=provenance, issues=issues)


def _sum_window(days: dict, end: date, duration: int) -> float | None:
    values = [days.get(end - timedelta(days=i), {}).get("precip") for i in range(duration)]
    return sum(values) if all(v is not None for v in values) else None
