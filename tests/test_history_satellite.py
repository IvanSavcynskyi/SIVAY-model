from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app.adapters.satellite import SatelliteProvider, filter_observations
from app.domain import CropHistoryRecord, SatelliteSeries
from app.features.history import apply_satellite_consistency, normalize_history
from app.features.satellite import build_satellite_features


def satellite_payload():
    return {
        "field_id": "field-a",
        "index": "NDVI",
        "sensor": "Sentinel-2 L2A",
        "native_resolution_m": 10,
        "processing": {"cloud_mask": "SCL", "version": "fixture-1"},
        "observations": [
            {
                "date": "2026-08-20",
                "acquired_at": "2026-08-20T10:00:00Z",
                "available_at": "2026-08-21T04:00:00Z",
                "valid_pixel_fraction": 0.95,
                "stats": {"mean": 0.6, "median": 0.62, "p10": 0.3, "p90": 0.8, "std": 0.1},
            }
        ],
    }


def test_satellite_filters_availability_acquisition_and_missing_timestamps():
    payload = satellite_payload()
    valid = payload["observations"][0]
    for update in (
        {"available_at": "2026-09-03T00:00:00Z"},
        {"acquired_at": "2026-09-03T00:00:00Z"},
        {"available_at": None},
        {"available_at": "2026-08-21T04:00:00"},
        {"date": "2026-08-19"},
    ):
        payload["observations"].append({**deepcopy(valid), **update})
    untouched = deepcopy(payload)
    result = filter_observations(payload, date(2026, 9, 1))
    assert result["observations"] == [valid]
    assert result["temporal_rejected_count"] == 5
    assert result["processing"] == payload["processing"]
    assert result["sensor"] == "Sentinel-2 L2A"
    assert result["native_resolution_m"] == 10
    assert payload == untouched


def test_datetime_cutoff_does_not_include_later_publication_on_same_day():
    result = filter_observations(
        satellite_payload(), datetime(2026, 8, 21, 3, tzinfo=timezone.utc)
    )
    assert result["observations"] == []


def test_satellite_provider_clamps_request_and_verifies_field():
    calls = []

    def fetch(**kwargs):
        calls.append(kwargs)
        return satellite_payload()

    provider = SatelliteProvider(fetch)
    result = provider.get("field-a", date(2026, 8, 1), date(2026, 12, 1), date(2026, 9, 1))
    assert calls[0]["to_date"] == date(2026, 9, 1)
    assert len(result.observations) == 1
    assert result.processing_version == "fixture-1"
    with pytest.raises(ValueError, match="field/index"):
        provider.get("field-b", date(2026, 8, 1), date(2026, 12, 1), date(2026, 9, 1))


def test_history_preserves_multiple_roles_and_explicit_and_implicit_gaps():
    rows = [
        {"season_year": 2024, "crop": " WHEAT ", "variant": "winter", "role": "main", "reliability": "high"},
        {"season_year": 2024, "crop": "soybean", "role": "second_crop", "reliability": "high"},
        {"season_year": 2024, "crop": "mustard", "role": "cover_crop", "reliability": "high"},
        {"season_year": 2026, "crop": "sunflower", "reliability": "high"},
    ]
    result = normalize_history(rows, [{"season_year": 2023, "reason": "unknown"}], date(2026, 9, 1))
    assert len(result.records) == 4
    assert [row.role for row in result.records[:3]] == ["main", "second_crop", "cover_crop"]
    assert result.records[0].crop == "wheat"
    assert result.records[0].variant == "winter"
    assert {gap.season_year for gap in result.gaps} == {2023, 2025}
    assert result.quality == "medium"
    assert result.occupied is None


def test_future_actual_harvest_and_yield_are_removed_but_known_plans_remain():
    row = CropHistoryRecord(
        season_year=2026, crop="sunflower", status="in_field", reliability="high",
        sowing_date=date(2026, 4, 30), harvest_date=date(2026, 10, 1), yield_t_ha=3.2,
        harvest_date_planned={"from": "2026-09-20", "to": "2026-10-05"},
    )
    result = normalize_history([row], [], date(2026, 9, 1))
    assert result.records[0].harvest_date is None
    assert result.records[0].yield_t_ha is None
    assert result.records[0].harvest_date_planned.to == date(2026, 10, 5)
    assert result.current_crop.crop == "sunflower"
    assert result.occupied is True
    assert row.harvest_date == date(2026, 10, 1)


def test_future_history_rows_and_unavailable_records_do_not_enter_snapshot():
    result = normalize_history([
        {"season_year": 2025, "crop": "soybean", "available_at": "2026-10-01T00:00:00Z"},
        {"season_year": 2027, "crop": "sunflower", "sowing_date": "2027-04-01"},
        {"season_year": 2027, "crop": "wheat", "sowing_date": "2026-08-30", "status": "in_field"},
    ], [], date(2026, 9, 1))
    assert len(result.records) == 1
    assert result.records[0].crop == "wheat"
    assert result.records[0].season_year == 2027


def test_satellite_qc_does_not_invent_phenology_or_increase_history_reliability():
    history = normalize_history([
        {"season_year": 2025, "crop": "sunflower", "reliability": "low"}
    ], [], date(2026, 9, 1))
    payload = satellite_payload()
    satellite = build_satellite_features(payload, date(2026, 9, 1))
    updated = apply_satellite_consistency(history, satellite)
    assert updated == history
    assert updated is not history
    assert updated.records[0].reliability == "low"
    assert satellite.obs_count_valid == 1
    assert satellite.effect_on_rank == "none"
    assert satellite.occupied is None
    assert satellite.median_valid_pixel_fraction == 0.95
    assert satellite.sensor == payload["sensor"]
    assert satellite.processing_version == "fixture-1"


@pytest.mark.parametrize("update", [
    {"valid_pixel_fraction": 0},
    {"valid_pixel_fraction": 1.1},
    {"stats": {"mean": float("nan"), "median": 0.5, "p10": 0.2, "p90": 0.8, "std": 0.1}},
    {"stats": {"mean": 0.5, "median": 0.9, "p10": 0.2, "p90": 0.8, "std": 0.1}},
])
def test_invalid_satellite_statistics_cannot_be_used_for_history(update):
    payload = satellite_payload()
    payload["observations"][0].update(update)
    result = build_satellite_features(payload, date(2026, 9, 1))
    assert result.obs_count_valid == 0
    assert result.quality == "low"
    assert "satellite_invalid_statistics" in result.issues


def test_missing_satellite_and_missing_metadata_do_not_establish_free_field():
    missing = build_satellite_features(None, date(2026, 9, 1))
    assert missing.quality == "missing"
    assert missing.occupied is None
    payload = satellite_payload()
    del payload["processing"]
    result = build_satellite_features(payload, date(2026, 9, 1))
    assert result.obs_count_valid == 0
    assert result.occupied is None
    assert result.processing_version is None
    payload = satellite_payload()
    payload["native_resolution_m"] = 0
    result = build_satellite_features(payload, date(2026, 9, 1))
    assert result.obs_count_valid == 0
    assert result.native_resolution_m is None


def test_normalized_satellite_series_has_same_qc_as_external_payload():
    series = SatelliteProvider(lambda **kwargs: satellite_payload()).get(
        "field-a", date(2026, 8, 1), date(2026, 9, 1), date(2026, 9, 1)
    )
    assert isinstance(series, SatelliteSeries)
    result = build_satellite_features(series, date(2026, 9, 1))
    assert result.obs_count_valid == 1
    assert result.processing_version == "fixture-1"


def test_missing_current_seasons_are_explicit_gaps_and_empty_history_is_missing():
    result = normalize_history([
        {"season_year": 2023, "crop": "wheat", "reliability": "high"}
    ], [], date(2026, 9, 1))
    assert {gap.season_year for gap in result.gaps} == {2024, 2025, 2026}
    assert result.quality != "high"
    assert normalize_history([], [], date(2026, 9, 1)).quality == "missing"


def test_month_precision_does_not_claim_harvest_before_month_has_ended():
    result = normalize_history([{
        "season_year": 2026, "crop": "wheat", "status": "harvested",
        "harvest_date": "2026-07-01", "harvest_date_precision": "month", "yield_t_ha": 5,
    }], [], date(2026, 7, 15))
    assert result.records[0].harvest_date is None
    assert result.records[0].yield_t_ha is None
    assert result.records[0].status == "unknown"
