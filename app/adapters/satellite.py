"""Platform satellite boundary with publication-time filtering.

The platform URL is deployment configuration. A caller supplies the transport;
tests and offline preparation can supply exactly the same platform payload.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timezone
from typing import Any, Callable, Mapping

from app.domain import SatelliteSeries


def as_of_cutoff(as_of: date | datetime | str) -> datetime:
    """Date-only requests include that entire UTC calendar day."""
    if isinstance(as_of, str):
        as_of = (
            datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if "T" in as_of or " " in as_of
            else date.fromisoformat(as_of)
        )
    if isinstance(as_of, datetime):
        if as_of.tzinfo is None:
            raise ValueError("as_of datetime must include a timezone")
        return as_of.astimezone(timezone.utc)
    return datetime.combine(as_of, time.max, tzinfo=timezone.utc)


def _instant(value: Any) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("satellite timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def filter_observations(
    series: Mapping[str, Any], as_of: date | datetime | str
) -> dict[str, Any]:
    """Keep only observations both acquired and published by the cutoff.

    Invalid or undated observations cannot be used in an as-of snapshot. Quality
    checks on pixel fractions and statistics are performed by the feature layer.
    """
    cutoff = as_of_cutoff(as_of)
    result = deepcopy(dict(series))
    observations = []
    rejected = 0
    for observation in series.get("observations", []):
        try:
            acquired = _instant(observation.get("acquired_at"))
            available = _instant(observation.get("available_at"))
            observed_date = observation.get("date")
            if isinstance(observed_date, str):
                observed_date = date.fromisoformat(observed_date)
            if not isinstance(observed_date, date) or isinstance(observed_date, datetime):
                raise ValueError("missing observation date")
            valid = (
                acquired <= available <= cutoff
                and acquired <= cutoff
                and observed_date <= cutoff.date()
                and observed_date == acquired.date()
            )
        except (TypeError, ValueError, AttributeError):
            valid = False
        if valid:
            observations.append(deepcopy(observation))
        else:
            rejected += 1
    observations.sort(key=lambda item: (str(item["date"]), str(item["acquired_at"])))
    result["observations"] = observations
    result["temporal_rejected_count"] = rejected
    return result


class SatelliteProvider:
    """Read an explicitly configured platform transport, never a guessed URL."""

    def __init__(self, fetch: Callable[..., Mapping[str, Any]]) -> None:
        self.fetch = fetch

    def get(
        self,
        field_id: str,
        from_date: date,
        to_date: date,
        as_of: date | datetime | str,
        index: str = "NDVI",
    ) -> SatelliteSeries:
        if index not in {"NDVI", "NDMI"}:
            raise ValueError("supported satellite indices are NDVI and NDMI")
        end = min(to_date, as_of_cutoff(as_of).date())
        if from_date > end:
            raise ValueError("satellite date range is empty")
        payload = self.fetch(
            field_id=field_id, index=index, from_date=from_date, to_date=end
        )
        if payload.get("field_id") != field_id or payload.get("index") != index:
            raise ValueError("satellite response does not match requested field/index")
        result = filter_observations(payload, as_of)
        result["observations"] = [
            item for item in result["observations"]
            if from_date <= date.fromisoformat(str(item["date"])) <= end
        ]
        return parse_satellite_series(result, as_of)


def parse_satellite_series(
    payload: Mapping[str, Any], as_of: date | datetime | str
) -> SatelliteSeries:
    """Normalize external nested stats/processing after filtering future rows."""
    filtered = filter_observations(payload, as_of)
    observations = []
    for item in filtered["observations"]:
        stats = item.get("stats", item)
        observations.append({
            key: item[key]
            for key in ("date", "acquired_at", "available_at", "valid_pixel_fraction")
        } | {
            key: stats.get(key) for key in ("mean", "median", "p10", "p90", "std")
        })
    return SatelliteSeries(
        index=payload.get("index", "NDVI"),
        sensor=payload["sensor"],
        native_resolution_m=payload["native_resolution_m"],
        processing_version=payload.get("processing_version") or payload.get("processing", {}).get("version"),
        observations=observations,
    )
