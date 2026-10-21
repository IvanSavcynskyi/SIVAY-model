"""Conservative normalization of declared field history at a fixed cutoff."""

from __future__ import annotations

from calendar import monthrange
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Iterable, Mapping

from app.adapters.satellite import _instant, as_of_cutoff
from app.domain import NormalizedHistory


def _mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python", by_alias=True)
    return deepcopy(dict(value))


def _date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _latest_date(record: Mapping[str, Any], name: str) -> date | None:
    value = _date(record.get(name))
    precision = record.get(f"{name}_precision", "day")
    if value and precision == "month":
        return value.replace(day=monthrange(value.year, value.month)[1])
    if value and precision == "season":
        return date(value.year, 12, 31)
    return value


def normalize_history(
    records: Iterable[Any],
    gaps: Iterable[Any] | None = None,
    as_of: date | datetime | str | None = None,
) -> NormalizedHistory:
    """Preserve multiple roles and distinguish observed facts from future plans.

    User-supplied rows without publication timestamps are declarations made for
    the requested cutoff. A timestamp, when supplied, always takes precedence.
    Future *actual* events and their yields are removed; planned harvest ranges
    remain useful for sowing-window feasibility.
    """
    if as_of is None:
        raise ValueError("history normalization requires as_of")
    cutoff = as_of_cutoff(as_of)
    day = cutoff.date()
    normalized: list[dict[str, Any]] = []
    issues: list[str] = []
    for position, value in enumerate(records):
        row = _mapping(value)
        record_id = row.get("record_id") or str(position)
        if row.get("available_at") is not None:
            try:
                if _instant(row["available_at"]) > cutoff:
                    issues.append(f"history_unavailable:{record_id}")
                    continue
            except (TypeError, ValueError):
                issues.append(f"history_invalid_available_at:{record_id}")
                continue
        crop = row.pop("crop_code", None) or row.get("crop")
        if not crop:
            issues.append(f"history_missing_crop:{record_id}")
            continue
        row["crop"] = str(crop).strip().lower()
        if row.get("variant") is not None:
            row["variant"] = row["variant"].strip().lower()
        row.setdefault("role", "main")
        row.setdefault("source", "user_memory")
        row.setdefault("reliability", "low")
        sowing = _latest_date(row, "sowing_date")
        harvest = _latest_date(row, "harvest_date")
        if sowing and sowing > day:
            issues.append(f"history_future_sowing:{record_id}")
            continue
        if row["season_year"] > day.year and sowing is None:
            issues.append(f"history_future_season:{record_id}")
            continue
        if harvest and harvest > day:
            row["harvest_date"] = None
            row["yield_t_ha"] = None
            issues.append(f"history_future_harvest_removed:{record_id}")
            if row.get("status") == "harvested":
                row["status"] = "in_field" if sowing else "unknown"
        if (row["season_year"] > day.year or row.get("status") == "in_field"
            or (row["season_year"] == day.year and not row.get("harvest_date")
                and row.get("status") != "harvested")):
            if row.get("yield_t_ha") is not None:
                issues.append(f"history_unobserved_yield_removed:{record_id}")
            row["yield_t_ha"] = None
        normalized.append(row)

    role_order = {"main": 0, "second_crop": 1, "cover_crop": 2}
    normalized.sort(key=lambda row: (
        row["season_year"], role_order.get(row["role"], 3),
        str(row.get("sowing_date") or ""), row["crop"], str(row.get("record_id") or ""),
    ))
    known_gaps = [_mapping(gap) for gap in (gaps or [])]
    known_gaps = [gap for gap in known_gaps if gap["season_year"] <= day.year]
    known_gaps.sort(key=lambda gap: (gap["season_year"], gap.get("reason") or ""))
    if normalized:
        years = {row["season_year"] for row in normalized}
        declared_gap_years = {gap["season_year"] for gap in known_gaps}
        for year in range(min(years), day.year + 1):
            if year not in years and year not in declared_gap_years:
                known_gaps.append({"season_year": year, "reason": "not_provided"})
        known_gaps.sort(key=lambda gap: (gap["season_year"], gap.get("reason") or ""))

    levels = {"low": 0, "medium": 1, "high": 2}
    level = min((levels[row["reliability"]] for row in normalized), default=0)
    if known_gaps:
        level = min(level, 1)
        issues.append("history_gaps")
    if not normalized:
        issues.append("history_missing")
    # Absence of a current observation cannot establish that the field is free.
    in_field = [row for row in normalized if row.get("status") == "in_field"]
    occupied = True if in_field else None
    current_crop = in_field[-1] if in_field else None
    return NormalizedHistory(
        records=normalized,
        gaps=known_gaps,
        occupied=occupied,
        current_crop=current_crop,
        issues=issues,
        quality=("low", "medium", "high")[level] if normalized else "missing",
    )


def apply_satellite_consistency(history: NormalizedHistory, satellite: Any) -> NormalizedHistory:
    """No reliability changes without approved, validated phenology rules.

    The design's N observations and seasonality cutoffs are uncalibrated. Merely
    having a valid NDVI series is not evidence supporting a particular crop.
    """
    return history.model_copy(deep=True)
