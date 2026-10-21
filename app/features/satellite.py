"""Satellite QC, deliberately separate from crop scoring and crop identity."""

from __future__ import annotations

from datetime import date, datetime
from math import isfinite
from statistics import median
from typing import Any, Mapping

from app.adapters.satellite import filter_observations
from app.domain import SatelliteFeatures, SatelliteSeries


def _valid_stats(observation: Mapping[str, Any]) -> bool:
    stats = observation.get("stats", observation)
    try:
        fraction = observation["valid_pixel_fraction"]
        if not isfinite(fraction) or not 0 < fraction <= 1:
            return False
        values = [stats.get(key) for key in ("mean", "median", "p10", "p90")]
        if any(value is None or not isfinite(value) or not -1 <= value <= 1 for value in values):
            return False
        if not stats["p10"] <= stats["median"] <= stats["p90"]:
            return False
        std = stats.get("std")
        return std is not None and isfinite(std) and std >= 0
    except (TypeError, KeyError):
        return False


def build_satellite_features(
    series: SatelliteSeries | Mapping[str, Any] | None,
    as_of: date | datetime | str,
) -> SatelliteFeatures:
    """Report usable NDVI coverage without inventing phenology thresholds.

    No approved classifier or vegetation cutoff exists in the supplied rules.
    Therefore the series neither identifies a crop nor asserts occupied/free.
    """
    if series is None:
        return SatelliteFeatures(
            quality="missing", issues=["satellite_missing"],
            notes=["Супутникова перевірка історії недоступна."],
        )
    payload = series.model_dump(mode="python") if isinstance(series, SatelliteSeries) else dict(series)
    filtered = filter_observations(payload, as_of)
    issues = []
    if filtered["temporal_rejected_count"]:
        issues.append("satellite_temporal_observations_rejected")
    processing_version = payload.get("processing_version") or payload.get("processing", {}).get("version")
    metadata_valid = bool(
        isinstance(payload.get("sensor"), str) and payload["sensor"].strip()
        and isinstance(processing_version, str) and processing_version.strip()
        and isinstance(payload.get("native_resolution_m"), (int, float))
        and isfinite(payload["native_resolution_m"]) and payload["native_resolution_m"] > 0
    )
    if not metadata_valid:
        issues.append("satellite_metadata_missing_or_invalid")
    observations = [item for item in filtered["observations"] if _valid_stats(item)]
    if len(observations) != len(filtered["observations"]):
        issues.append("satellite_invalid_statistics")
    if payload.get("index", "NDVI") != "NDVI":
        issues.append("satellite_ndvi_missing")
        observations = []
    if not metadata_valid:
        observations = []
    notes = [
        "Супутникові індекси не змінюють агрономічну оцінку напряму.",
        "Фенологічна перевірка та визначення зайнятості потребують затверджених і перевірених правил.",
    ]
    if not observations:
        notes.append("Перевірити історію за супутником неможливо.")
    available = max(
        (datetime.fromisoformat(item["available_at"].replace("Z", "+00:00"))
         if isinstance(item["available_at"], str) else item["available_at"]
         for item in observations),
        default=None,
    )
    result = {
        "quality": "medium" if observations else "low",
        "obs_count_valid": len(observations),
        "occupied": None,
        "issues": issues,
        "notes": notes,
        "available_at": available,
        "sensor": payload.get("sensor") if metadata_valid else None,
        "native_resolution_m": payload.get("native_resolution_m") if metadata_valid else None,
        "processing_version": processing_version if metadata_valid else None,
        "median_valid_pixel_fraction": median(item["valid_pixel_fraction"] for item in observations) if observations else None,
    }
    return SatelliteFeatures(**result)
