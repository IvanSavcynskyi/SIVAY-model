"""Personalization changes decisions, filters bad evidence, and replays sealed inputs."""
import base64
import json
from datetime import date, datetime, timedelta, timezone

import numpy as np
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from app.config import Settings
from app.domain import (CropVariant, FieldImageInput, FieldSignals, FieldWeatherInput,
                        PersonalizationPolicy, SatelliteSeries, WeatherRecord)
from app.explain import enrich_result
from app.features.imagery import image_indices, validate_image
from app.features.personalization import build_index_signals, build_local_weather
from app.geo import validate_geometry
from app.main import create_app
from app.ranking.personalized import recommend_personalized
from app.service import RecommendationService
from test_gaez import make_store
from test_ranking import layer, request, rules, snapshot

DAY = date(2026, 9, 1)


def policy(**updates):
    data = {"version": "synthetic-farm-v1", "source": "synthetic test policy",
            "avoid_same_crop_predecessor": False}
    data.update(updates)
    return PersonalizationPolicy.model_validate(data)


def series(index="NDMI", mean=.2, day="2026-08-31", **changes):
    observation = {"date": day, "acquired_at": day + "T10:00:00Z",
                   "available_at": day + "T12:00:00Z", "valid_pixel_fraction": .95, "mean": mean}
    observation.update(changes)
    return SatelliteSeries(index=index, sensor="synthetic", native_resolution_m=10,
                           processing_version="v1", observations=[observation])


def image_payload(clear=True, zero_nir=False, image_id="scene1"):
    values = np.stack([np.full((2, 2), 1000), np.full((2, 2), 3000 if not zero_nir else 0),
                       np.full((2, 2), 2000), np.full((2, 2), 1 if clear else 0)]).astype("uint16")
    with MemoryFile() as memory:
        with memory.open(driver="GTiff", width=2, height=2, count=4, dtype="uint16",
                         crs="EPSG:4326", transform=from_origin(34.5, 49.61, .005, .005)) as dst:
            dst.write(values)
        encoded = base64.b64encode(memory.read()).decode()
    return FieldImageInput(image_id=image_id, data_base64=encoded, acquired_at="2026-08-31T10:00:00Z",
        available_at="2026-08-31T12:00:00Z", sensor="synthetic-scene", processing_version="v1",
        bands={"red": 1, "nir": 2, "swir": 3, "quality": 4}, scale=.0001, clear_values=[1])


def field_geometry():
    from shapely.geometry import box
    return validate_geometry(box(34.5, 49.6, 34.51, 49.61), "EPSG:4326")


def test_geotiff_indices_radiometry_mask_and_field_intersection():
    entries, metadata = image_indices(image_payload(), field_geometry())
    assert {s.index for s in entries} == {"NDVI", "NDMI"}
    values = {s.index: s.observations[0] for s in entries}
    assert values["NDVI"].mean == pytest.approx(.5)
    assert values["NDMI"].mean == pytest.approx(.2)
    assert values["NDMI"].valid_pixel_fraction == pytest.approx(1)
    assert metadata["sha256"] == validate_image(image_payload())
    with pytest.raises(ValueError, match="NO_VALID"):
        image_indices(image_payload(clear=False), field_geometry())
    # Half-field quality mask halves coverage, rather than reporting clear-pixel coverage only.
    from shapely.geometry import box
    extended = validate_geometry(box(34.5, 49.6, 34.52, 49.61), "EPSG:4326")
    partial, _ = image_indices(image_payload(), extended)
    assert partial[0].observations[0].valid_pixel_fraction == pytest.approx(.5)


def test_image_rejects_non_tiff_missing_mask_and_wrong_field():
    bad = image_payload().model_copy(update={"data_base64": base64.b64encode(b"<VRTDataset/>").decode()})
    with pytest.raises(ValueError, match="EXPECTED_GEOTIFF"):
        validate_image(bad)
    data = image_payload().model_dump()
    data["bands"].pop("quality")
    with pytest.raises(ValidationError, match="quality-mask"):
        FieldImageInput.model_validate(data)
    with pytest.raises(ValueError, match="DOES_NOT_COVER"):
        image_indices(image_payload(), validate_geometry(request().field.geometry, request().field.crs))


def test_both_platform_indices_mean_only_and_compatible_trends():
    signals = FieldSignals()
    entries = [(series("NDMI", .1), "current"), (series("NDMI", .3, "2026-08-25"), "previous"),
               (series("NDVI", .6), "ndvi")]
    build_index_signals(entries, DAY, policy(), signals)
    assert signals.metrics["NDMI.delta"] == pytest.approx(-.2)
    assert signals.metrics["NDVI.mean"] == .6
    assert signals.metric_sources["NDMI.delta"] == ["previous", "current"]
    entries[1][0].processing_version = "other"
    signals = FieldSignals()
    build_index_signals(entries, DAY, policy(), signals)
    assert "NDMI.delta" not in signals.metrics


@pytest.mark.parametrize("entry,issue", [
    (series(valid_pixel_fraction=.2), "LOW_COVERAGE"),
    (series(day="2026-07-01"), "STALE"),
    (series(available_at="2026-09-02T00:00:00Z"), "TEMPORAL_REJECTED"),
    (series(mean=3), "INVALID_MEAN"),
])
def test_bad_index_never_supplies_a_ranking_metric(entry, issue):
    signals = FieldSignals()
    build_index_signals([(entry, "bad")], DAY, policy(), signals)
    assert not signals.metrics
    assert any(issue in item for item in signals.issues)


def weather_input(missing_day=False, forecast=False):
    records = []
    for i in range(1, 31):
        if missing_day and i == 4:
            continue
        day = DAY - timedelta(days=i)
        records.append(WeatherRecord(valid_time=f"{day}T00:00:00Z", available_at=f"{day}T23:00:00Z",
            kind="observation", provider="synthetic-station",
            values={"precip": 1, "et0": 2, "tmin": 5, "tmax": 25, "soil_moisture_0_7": .2}))
    if forecast:
        for i in range(1, 8):
            day = DAY + timedelta(days=i)
            records.append(WeatherRecord(valid_time=f"{day}T00:00:00Z", available_at="2026-09-01T12:00:00Z",
                issued_at="2026-09-01T06:00:00Z", kind="forecast", provider="synthetic-forecast",
                values={"precip": 2, "tmin": -2, "tmax": 20}))
    return FieldWeatherInput(latitude=49.605, longitude=34.505, resolution_m=10, records=records)


def test_weather_uses_complete_windows_nearby_location_and_archived_forecasts():
    signals = FieldSignals()
    build_local_weather(weather_input(forecast=True), field_geometry(), DAY, policy(), signals, "station")
    assert signals.metrics["weather.precip_7d"] == 7
    assert signals.metrics["weather.balance_30d"] == -30
    assert signals.metrics["forecast.tmin_7d"] == -2
    assert signals.metrics["forecast.precip_7d"] == 14
    assert signals.weather["kind"] == "observation"
    incomplete = FieldSignals()
    build_local_weather(weather_input(missing_day=True), field_geometry(), DAY, policy(), incomplete, "station")
    assert "weather.precip_7d" not in incomplete.metrics
    far = weather_input().model_copy(update={"latitude": 48})
    with pytest.raises(ValueError, match="TOO_FAR"):
        build_local_weather(far, field_geometry(), DAY, policy(), FieldSignals(), "far")


def test_forecast_without_verified_issuance_cannot_supply_personalization_metrics():
    data = weather_input(forecast=True)
    for record in data.records:
        if record.kind == "forecast":
            record.issued_at = None
    signals = FieldSignals()
    build_local_weather(data, field_geometry(), DAY, policy(), signals, "station")
    assert not any(name.startswith("forecast.") for name in signals.metrics)
    assert signals.metrics["weather.precip_7d"] == 7


def synthetic_candidates(count=6):
    crops = [CropVariant(id=f"crop{i}", name_uk=f"Культура {i}", family="synthetic", season_type="winter",
                         gaez_code=f"C{i}", status="demo") for i in range(count)]
    snap = snapshot(gaez={c.gaez_code: [layer(code=c.gaez_code, value=9000-i*100)] for i, c in enumerate(crops)})
    return crops, snap, rules(crops)


def test_indices_can_promote_sixth_candidate_and_explanations_are_traceable():
    crops, snap, bundle = synthetic_candidates()
    snap.field_signals = FieldSignals(policy=policy(rules=[{
        "id": "test-ndmi", "crops": [c.id for c in crops[:5]], "metric": "NDMI.mean",
        "operator": "lt", "threshold": .1, "source": "synthetic requirement",
        "explanation_uk": "Низький NDMI за заданим правилом."}]), metrics={"NDMI.mean": -.2},
        metric_dates={"NDMI.mean": DAY}, metric_sources={"NDMI.mean": ["platform"]},
        weather={"sowing_dates": {c.id: "2026-09-05" for c in crops}})
    req = request()
    result = enrich_result(recommend_personalized(req, snap, bundle))
    assert result.results[0].crop_variant == "crop5"
    assert len(result.results) == 5
    assert result.results[1].personalization.risk_priority == 1
    assert result.results[1].agro_score.mid == .9
    assert "Низький NDMI" in " ".join(e.text for e in result.results[1].reasons)
    assert enrich_result(result) == result
    assert recommend_personalized(req, snap, bundle).model_dump_json() == recommend_personalized(req, snap, bundle).model_dump_json()


def test_rotation_return_intervals_and_farm_predecessors_change_order():
    crops, snap, bundle = synthetic_candidates(2)
    from app.features.history import normalize_history
    snap.history = normalize_history([{"crop": "crop0", "season_year": 2026, "reliability": "high"}], [], DAY)
    snap.field_signals = FieldSignals(policy=policy(avoid_same_crop_predecessor=True, min_return_years={"crop0": 3}))
    result = recommend_personalized(request(), snap, bundle)
    assert result.results[0].crop_variant == "crop1"
    repeated = result.results[1].personalization
    assert repeated.risk_priority == 3
    assert any(a.rule_id == "rotation.return" and a.outcome == "risk" for a in repeated.assessments)


def test_current_weather_does_not_predict_next_season_and_missing_data_is_unknown():
    crops, snap, bundle = synthetic_candidates(1)
    snap.field_signals = FieldSignals(policy=policy(rules=[{
        "id": "frost", "crops": ["crop0"], "metric": "forecast.tmin_7d", "operator": "lt",
        "threshold": 0, "priority": 3, "source": "synthetic", "explanation_uk": "Ризик морозу."}]),
        metrics={"forecast.tmin_7d": -3}, weather={"sowing_dates": {"crop0": "2027-04-01"}})
    from app.domain import TargetSeason
    next_year = request().model_copy(update={"target_season": TargetSeason(harvest_year=2028, include=["winter"])})
    far = recommend_personalized(next_year, snap, bundle).results[0].personalization
    assert far.assessments[0].outcome == "not_applicable" and far.risk_priority == 0
    snap.field_signals.weather["sowing_dates"]["crop0"] = "2026-09-03"
    assert recommend_personalized(request(), snap, bundle).results[0].personalization.risk_priority == 3
    snap.field_signals.metrics.clear()
    missing = recommend_personalized(request(), snap, bundle).results[0].personalization
    assert missing.assessments[0].outcome == "unknown" and missing.risk_priority == 3


def test_variant_specific_rules_do_not_apply_to_other_season_and_bad_plan_is_unknown():
    crops = [CropVariant(id="wheat_winter", name_uk="Озима", family="cereals", season_type="winter",
                         gaez_code="WHE", status="demo"),
             CropVariant(id="wheat_spring", name_uk="Яра", family="cereals", season_type="spring",
                         gaez_code="WHE", status="demo")]
    snap = snapshot(field_signals=FieldSignals(policy=policy(rules=[{
        "id": "winter-only", "crops": ["wheat_winter"], "metric": "NDMI.mean", "operator": "lt",
        "threshold": .1, "priority": 2, "source": "synthetic", "explanation_uk": "Перевірка."}]),
        metrics={"NDMI.mean": -.2}, weather={"sowing_dates": {"wheat_winter": "2027-09-05"}}))
    result = recommend_personalized(request(), snap, rules(crops))
    rows = {c.crop_variant: c.personalization for c in result.results}
    assert not rows["wheat_spring"].assessments
    assert rows["wheat_winter"].assessments[0].outcome == "unknown"
    assert "сезону" in rows["wheat_winter"].assessments[0].explanation_uk


def test_non_demo_ingress_image_ndmi_weather_policy_replay_and_ingestion_cutoff(tmp_path, monkeypatch):
    import app.api.routes as routes
    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(routes, "datetime", FrozenClock)
    make_store(tmp_path, available_at="2026-08-01T00:00:00Z")
    boundary = tmp_path / "regions.geojson"
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [{"type": "Feature",
        "properties": {"region": "UA"}, "geometry": {"type": "Polygon", "coordinates": [
            [[34, 49], [36, 49], [36, 50], [34, 50], [34, 49]]]}}]}))
    service = RecommendationService(Settings(database_url=f"sqlite:///{tmp_path / 'personal.db'}",
        gaez_root=tmp_path, regions_path=boundary))
    payload = request().model_dump(mode="json", by_alias=True)
    payload["field"]["geometry"] = field_geometry().geojson
    field = payload["field"]["field_id"]
    with TestClient(create_app(service=service)) as client:
        assert service.profile == "personalized"
        assert client.get("/openapi.json").status_code == 200
        invalid = {"policy": policy(min_return_years={"typo-crop": 3}).model_dump(mode="json")}
        assert client.put(f"/v1/fields/{field}/personalization", json=invalid).status_code == 422
        assert client.post("/v1/recommendations", json={**payload, "personalization": invalid}).status_code == 422
        assert client.post(f"/v1/fields/{field}/images", json=image_payload().model_dump(mode="json")).status_code == 201
        assert "data_base64" not in client.get(f"/v1/fields/{field}/images").text
        assert client.post(f"/v1/fields/{field}/indices", json=series(mean=-.2).model_dump(mode="json")).status_code == 201
        assert client.put(f"/v1/fields/{field}/weather", json=weather_input().model_dump(mode="json")).status_code == 200
        payload["target_season"]["include"] = ["winter"]
        farm = {"sowing_dates": {"wheat_winter": "2026-09-05"}, "policy": policy(rules=[{
            "id": "ndmi", "crops": ["wheat_winter"], "metric": "NDMI.mean", "operator": "lt",
            "threshold": 0, "source": "synthetic-farm", "explanation_uk": "Низький NDMI."}]).model_dump(mode="json")}
        assert client.put(f"/v1/fields/{field}/personalization", json=farm).status_code == 200
        response = client.post("/v1/recommendations", json=payload)
        assert response.status_code == 200, response.text
        first = response.json()
        row = first["results"][0]
        assert row["personalization"]["metrics"]["NDMI.mean"] == -.2  # supplied index wins over imagery
        assert row["personalization"]["metrics"]["NDVI.mean"] == pytest.approx(.5)
        assert row["personalization"]["metrics"]["weather.precip_7d"] == 7
        assert row["personalization"]["risk_priority"] == 1
        assert first["provenance"]["algorithm_version"] == "personalized-1.0.0"
        snap = first["provenance"]["snapshot_id"]
        assert client.post("/v1/recommendations", params={"snapshot_id": snap}, json=payload).json() == first
        assert client.get("/v1/recommendations/" + first["recommendation_id"]).json() == first
        # New evidence creates a new snapshot but cannot mutate the previous one.
        assert client.post(f"/v1/fields/{field}/indices", json=series(mean=.5).model_dump(mode="json")).status_code == 201
        updated = client.post("/v1/recommendations", json=payload).json()
        assert updated["provenance"]["snapshot_id"] != snap
        assert updated["results"][0]["personalization"]["risk_priority"] == 0
        assert client.post("/v1/recommendations", params={"snapshot_id": snap}, json=payload).json() == first
        old = client.post("/v1/recommendations", json={**payload, "as_of_date": "2026-08-31"}).json()
        assert old["results"][0]["personalization"]["evidence"]["images"] == []
