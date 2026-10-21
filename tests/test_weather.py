import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from app.adapters.weather import FileWeatherProvider, OpenMeteoProvider, available_records
from app.domain import Interval, RankingParameters, RuleSource, WeatherRecord
from app.features.weather import build_weather_features, empirical_percentile


def dt(value):
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


AS_OF = dt("2026-09-02T12:00:00")


def record(day, values=None, **kw):
    return WeatherRecord(valid_time=dt(day + "T00:00:00"),
        available_at=kw.pop("available_at", dt(day + "T00:00:00") + timedelta(days=1)),
        provider=kw.pop("provider", "fixture:era5_land"), kind=kw.pop("kind", "reanalysis"),
        values=values or {"precip": 1, "soil_moisture_0_7": .2}, **kw)


def full_records():
    rows = []
    for year in [*range(1991, 2021), 2026]:
        end = date(year, 9, 1)
        for i in range(30):
            day = end - timedelta(days=i)
            values = {"precip": float(year - 1990) if year != 2026 else .1,
                      "soil_moisture_0_7": (year - 1990) / 100 if year != 2026 else .001}
            rows.append(record(day.isoformat(), values))
    return rows


def approved():
    return RankingParameters(status="approved",
        source=RuleSource(ref="test-fixture", verified_by="test agronomist", verified_at=date(2026, 1, 1)),
        weather_dry_percentile=20, weather_wet_percentile=60,
        weather_multipliers={"dry": Interval(lo=.8, mid=.9, hi=1)})


def test_future_availability_issue_and_horizon_are_filtered():
    rows = [record("2026-09-01"),
            record("2026-09-01", available_at=AS_OF + timedelta(seconds=1)),
            record("2026-09-05", kind="forecast", issued_at=AS_OF + timedelta(seconds=1), available_at=AS_OF),
            record("2026-09-05", kind="forecast", available_at=AS_OF),
            record("2026-09-30", kind="forecast", issued_at=AS_OF, available_at=AS_OF),
            record("2026-09-05", kind="forecast", issued_at=AS_OF - timedelta(hours=1), available_at=AS_OF)]
    result = available_records(rows, AS_OF)
    assert len(result) == 2
    assert result[-1].valid_time.day == 5


def test_complete_same_provider_baselines_and_reviewed_signal():
    feature = build_weather_features(full_records(), AS_OF, approved())
    assert feature.moisture_percentile == feature.precipitation_percentile == 0
    assert feature.signal == "dry"
    assert feature.multiplier.mid == .9
    assert feature.available_at <= AS_OF
    assert feature.provenance[0]["baseline_precipitation_years"] == 30


def test_draft_thresholds_and_hazards_do_not_invent_calibration():
    params = approved().model_copy(update={"status": "draft"})
    feature = build_weather_features(full_records(), AS_OF, params)
    assert feature.moisture_percentile == 0
    assert feature.signal == "unknown" and feature.multiplier is None
    assert all(r.level == "unknown" for r in feature.risks)


def test_future_normal_cannot_be_used_in_historical_backtest():
    rows = full_records()
    for row in rows:
        if row.valid_time.year <= 2020:
            row.available_at = AS_OF + timedelta(seconds=1)
    feature = build_weather_features(rows, AS_OF, approved())
    assert feature.moisture_percentile is feature.precipitation_percentile is None
    assert feature.signal == "unknown" and feature.multiplier is None


def test_mixed_provider_and_incomplete_windows_are_missing():
    rows = full_records()
    for row in rows:
        if row.valid_time.year == 1991:
            row.provider = "other:model"
    feature = build_weather_features(rows, AS_OF, approved())
    assert feature.moisture_percentile is feature.precipitation_percentile is None
    assert "PRECIPITATION_BASELINE_OR_CURRENT_INCOMPLETE" in feature.issues
    assert build_weather_features([record("1991-09-01")], AS_OF).quality == "missing"


def test_future_forecast_never_overwrites_actual_state():
    rows = full_records()
    rows.append(record("2026-09-04", {"precip": 100, "soil_moisture_0_7": 1},
        kind="forecast", issued_at=AS_OF, available_at=AS_OF))
    feature = build_weather_features(rows, AS_OF, approved())
    assert feature.signal == "dry"
    assert feature.provenance[0]["forecast_records_available"] == 1


def test_file_provider_timestamps_coordinates_and_defensive_copies(tmp_path):
    path = tmp_path / "weather.json"
    rows = [record("2026-09-01"), record("2026-09-01", available_at=AS_OF + timedelta(days=1))]
    path.write_text(json.dumps({"latitude": 49.5, "longitude": 34.5,
        "records": [r.model_dump(mode="json") for r in rows]}))
    provider = FileWeatherProvider(path)
    assert len(provider.records_for(49.5, 34.5, AS_OF)) == 1
    with pytest.raises(ValueError, match="LOCATION_MISMATCH"):
        provider.records_for(50, 34.5, AS_OF)
    found = provider.get("reanalysis", 49.5, 34.5, ["precip"], (date(2026, 9, 1), date(2026, 9, 1)), AS_OF)
    found[0].values["precip"] = 999
    assert provider.records_for(49.5, 34.5, AS_OF)[0].values["precip"] == 1


def test_live_disabled_and_historical_asof_do_not_call_network():
    def forbidden(request):
        raise AssertionError("network must not be called")
    query = ("reanalysis", 49, 34, ["precip"], (date(2020, 1, 1), date(2020, 1, 1)), date(2020, 1, 2))
    with pytest.raises(ValueError, match="NETWORK_DISABLED"):
        OpenMeteoProvider(transport=httpx.MockTransport(forbidden)).get(*query)
    live = OpenMeteoProvider(enabled=True, transport=httpx.MockTransport(forbidden), clock=lambda: AS_OF)
    assert live.get(*query) == []


def test_live_retry_cache_missing_values_and_retrieval_availability():
    calls = []
    def handler(request):
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(503)
        assert request.url.params["models"] == "era5_land"
        return httpx.Response(200, json={"daily": {"time": ["2026-08-25"],
            "precipitation_sum": [None]}, "hourly": {"time": [f"2026-08-25T{i:02}:00" for i in range(24)],
            "soil_moisture_0_to_7cm": [.2] * 24}})
    provider = OpenMeteoProvider(enabled=True, transport=httpx.MockTransport(handler), clock=lambda: AS_OF, sleep=lambda _: None)
    query = ("reanalysis", 49, 34, ["precip", "soil_moisture_0_7"],
             (date(2026, 8, 25), date(2026, 8, 25)), AS_OF)
    result = provider.get(*query)
    assert len(calls) == 3 and len(result) == 1
    assert result[0].available_at == AS_OF
    assert result[0].values["precip"] is None
    assert result[0].values["soil_moisture_0_7"] == pytest.approx(.2)
    result[0].values["soil_moisture_0_7"] = 2
    assert provider.get(*query)[0].values["soil_moisture_0_7"] == pytest.approx(.2)
    assert len(calls) == 3


def test_live_does_not_guess_forecast_issuance():
    provider = OpenMeteoProvider(enabled=True, clock=lambda: AS_OF)
    with pytest.raises(ValueError, match="ISSUED_AT_UNAVAILABLE"):
        provider.get("forecast", 49, 34, ["precip"], (date(2026, 9, 3), date(2026, 9, 4)), AS_OF)


def test_http_client_error_is_not_retried():
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(400)
    provider = OpenMeteoProvider(enabled=True, clock=lambda: AS_OF, transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        provider.get("reanalysis", 49, 34, ["precip"], (date(2026, 8, 1), date(2026, 8, 1)), AS_OF)
    assert len(calls) == 1


def test_percentile_ties_and_unknown():
    assert empirical_percentile(1, [1, 1, 1]) == 50
    assert empirical_percentile(1, []) is None
