"""Cross-module diagnostics and conservative nationwide MVP policy checks.

All fixtures here are synthetic; no agronomist has certified these crop lists.
"""
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest
from hypothesis import given, settings, strategies as st

from app.domain.models import (
    EconomicsInput, NormalizedHistory, SatelliteFeatures, WeatherFeatures,
)
from app.ranking.baseline import recommend_baseline
from app.ranking.engine import recommend
from app.validation import ValidationCase, ablate, audit_cutoff, backtest, blind_package

FIXTURE = Path(__file__).parent / "fixtures" / "quality-cases.json"


def case():
    return ValidationCase.model_validate(json.loads(FIXTURE.read_text())["cases"][0])


def projection(result):
    return [(row.crop_variant, row.agro_score, row.status) for row in result.results]


def test_baseline_golden_is_conditional_source_potential_and_never_invents_yield():
    c = case()
    result = recommend_baseline(c.request, c.snapshot, c.rules)
    assert result.mode == "LIMITED"
    assert [row.crop_variant for row in result.results] == [
        "maize_grain", "barley_spring", "pea_dry", "sugar_beet"]
    assert [(row.agro_score.lo, row.agro_score.mid, row.agro_score.hi)
            for row in result.results] == [(.66, .74, .77), (.66, .72, .75),
                                         (.62, .68, .71), (.60, .70, .73)]
    for row in result.results:
        assert row.status == "conditional"
        assert row.rotation.multiplier.lo == row.weather.multiplier.lo == 1
        assert row.economics.yield_t_ha is None
        assert row.economics.margin_uah_ha is None
        assert not row.economics.applied_to_rank
        assert row.stability.label == "unavailable"
        assert row.stability.scenarios_evaluated == 3
        assert row.stability.main_driver == "spatial_GAEZ_endpoints_only"


@pytest.mark.parametrize("region", ["UA", "UA-05", "UA-14", "UA-21", "UA-43", "UA-53", "UA-80", "UA-85"])
def test_baseline_is_nationwide_without_claiming_approved_regional_agronomy(region):
    c = case()
    c.snapshot.region = region
    result = recommend_baseline(c.request, c.snapshot, c.rules)
    assert len(result.results) == 4
    assert result.mode == "LIMITED"
    assert all("AGRONOMY_NOT_VALIDATED" in row.data_quality.issues for row in result.results)


def test_baseline_ignores_unapproved_agronomic_numbers_and_missing_history():
    c = case()
    initial = recommend_baseline(c.request, c.snapshot, c.rules)
    c.rules.parameters.status = "draft"
    c.rules.parameters.floor_agro = .99
    c.rules.parameters.effect_multipliers = {"favorable": 1000, "neutral": .01}
    c.rules.sowing_windows = []
    c.snapshot.history = NormalizedHistory()
    c.snapshot.weather = WeatherFeatures(signal="dry", quality="high",
                                          available_at=c.snapshot.prepared_at)
    c.snapshot.satellite = SatelliteFeatures(quality="high", occupied=True, obs_count_valid=100)
    changed = recommend_baseline(c.request, c.snapshot, c.rules)
    assert projection(changed) == projection(initial)
    assert all(row.rotation.multiplier.lo == row.rotation.multiplier.hi == 1
               and row.weather.multiplier.lo == row.weather.multiplier.hi == 1
               for row in changed.results)


def test_baseline_pure_replay_and_input_order_independent(monkeypatch):
    import socket
    def forbidden(*args, **kwargs):
        raise AssertionError("ranker must never open a network connection")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    c = case()
    before = c.model_dump_json()
    first = recommend_baseline(c.request, c.snapshot, c.rules)
    assert recommend_baseline(c.request, c.snapshot, c.rules).model_dump_json() == first.model_dump_json()
    assert c.model_dump_json() == before
    c.rules.catalog.reverse()
    c.snapshot.gaez = dict(reversed(list(c.snapshot.gaez.items())))
    assert projection(recommend_baseline(c.request, c.snapshot, c.rules)) == projection(first)
    assert first.computed_at == c.snapshot.prepared_at


@given(st.floats(min_value=0, max_value=1, allow_nan=False, allow_infinity=False))
@settings(max_examples=30, deadline=None)
def test_decreasing_a_crop_suitability_cannot_improve_its_baseline_place(factor):
    c = case()
    before = recommend_baseline(c.request, c.snapshot, c.rules)
    identifier = "sugar_beet"
    crop = next(row for row in c.rules.catalog if row.id == identifier)
    layer = c.snapshot.gaez[crop.gaez_code][0]
    layer.mean_w *= factor
    for cell in layer.per_cell:
        cell.value *= factor
    after = recommend_baseline(c.request, c.snapshot, c.rules)
    old = next(row.rank for row in before.results if row.crop_variant == identifier)
    ranks = {row.crop_variant: row.rank for row in after.results}
    assert ranks.get(identifier, 6) >= old


@given(st.sampled_from(["maize_grain", "barley_spring", "sugar_beet", "pea_dry"]),
       st.floats(min_value=0, max_value=.799, allow_nan=False, allow_infinity=False))
@settings(max_examples=30, deadline=None)
def test_removing_own_gaez_coverage_cannot_improve_against_unaffected_crops(identifier, coverage):
    c = case()
    before = recommend_baseline(c.request, c.snapshot, c.rules)
    crop = next(row for row in c.rules.catalog if row.id == identifier)
    c.snapshot.gaez[crop.gaez_code][0].coverage_valid = coverage
    after = recommend_baseline(c.request, c.snapshot, c.rules)
    old = {row.crop_variant: row for row in before.results}[identifier]
    surviving = {row.crop_variant: row for row in after.results}
    if identifier in surviving:
        assert surviving[identifier].agro_score.lo <= old.agro_score.lo
        assert surviving[identifier].rank >= old.rank
    else:
        assert next(row for row in after.not_evaluated
                    if row.crop_variant == identifier).reason == "GAEZ_NO_COVERAGE"


@pytest.mark.parametrize("coverage,mode,issue", [
    (.49, "ABSTAIN", "GAEZ_NO_COVERAGE"),
    (.5, "LIMITED", "GAEZ_PARTIAL_COVERAGE"),
    (.79, "LIMITED", "GAEZ_PARTIAL_COVERAGE"),
    (.8, "LIMITED", None),
])
def test_missingness_coverage_boundaries_all_candidates(coverage, mode, issue):
    c = case()
    for layers in c.snapshot.gaez.values():
        for layer in layers:
            layer.coverage_valid = coverage
    result = recommend_baseline(c.request, c.snapshot, c.rules)
    assert result.mode == mode
    if coverage < .5:
        assert not result.results and {r.reason for r in result.not_evaluated} == {issue}
    elif issue:
        assert all(row.agro_score.lo == 0 and issue in row.data_quality.issues
                   for row in result.results)


@pytest.mark.parametrize("scenario,mode,reason", [
    ("missing_history", "LIMITED", "history_incomplete"),
    ("history_gap", "LIMITED", "history_incomplete"),
    ("missing_weather", "LIMITED", "weather_missing"),
    ("missing_price", "AGRO_ONLY", "ECON_PRICE_MISSING"),
    ("no_costs", "AGRO_ONLY", "ECON_COST_MISSING"),
    ("unsupported_region", "ABSTAIN", "REGION_NOT_SUPPORTED"),
    ("below_threshold", "ABSTAIN", "BELOW_AGRO_THRESHOLD"),
])
def test_validated_missingness_table_modes_and_reasons(scenario, mode, reason):
    c = case()
    if scenario == "missing_history":
        c.snapshot.history = NormalizedHistory(occupied=False)
    elif scenario == "history_gap":
        from app.domain.models import HistoryGap
        c.snapshot.history.gaps = [HistoryGap(season_year=2025)]
    elif scenario == "missing_weather":
        c.request.as_of_date = c.snapshot.as_of_date = date(2027, 4, 1)
        c.rules.catalog[0].establishment_moisture_sensitive = True
    elif scenario in {"missing_price", "no_costs"}:
        c.request.economics = EconomicsInput()
        if scenario == "no_costs":
            c.request.economics = EconomicsInput.model_validate({
                "expected_yield_t_ha": {crop.id: {"low": 1, "base": 2, "high": 3}
                                        for crop in c.rules.catalog},
                "contract_prices": [{"crop": crop.id, "price": 10000}
                                    for crop in c.rules.catalog],
            })
    elif scenario == "unsupported_region":
        c.snapshot.region = "UA-32"
    else:
        c.rules.parameters.floor_agro = .99
    result = recommend(c.request, c.snapshot, c.rules)
    assert result.mode == mode
    if scenario == "unsupported_region":
        assert {row.reason for row in result.not_evaluated} == {reason}
    elif scenario == "below_threshold":
        assert len(result.below_threshold) == len(c.rules.catalog)
        assert {row.reason for row in result.below_threshold} == {reason}
    elif scenario in {"missing_price", "no_costs"}:
        assert all(reason in row.economics.issues for row in result.results)
        assert all(not row.economics.applied_to_rank for row in result.results)
        if scenario == "no_costs":
            assert all(row.economics.revenue_uah_ha is not None
                       and row.economics.margin_uah_ha is None for row in result.results)
    else:
        assert any(reason in row.data_quality.issues for row in result.results)


def test_future_gaez_canary_cannot_change_ranking_or_used_sources():
    c = case()
    before = recommend_baseline(c.request, c.snapshot, c.rules)
    for code, layers in c.snapshot.gaez.items():
        canary = layers[0].model_copy(deep=True)
        canary.layer_code = f"FUTURE-CANARY-{code}"
        canary.mean_w = 10000
        canary.per_cell = []
        canary.available_at = c.snapshot.prepared_at + timedelta(days=1)
        layers.append(canary)
    after = recommend_baseline(c.request, c.snapshot, c.rules)
    assert projection(before) == projection(after)
    assert before.provenance.gaez_layers == after.provenance.gaez_layers
    assert all("CANARY" not in str(row) for row in after.provenance.gaez_layers)
    assert audit_cutoff(c)  # Backtest audit rejects, live ranker filters.


def test_baseline_missing_management_keeps_limitation_and_input_request():
    c = case()
    c.snapshot.assumptions.append("input_level=high (assumed)")
    result = recommend_baseline(c.request, c.snapshot, c.rules)
    assert result.mode == "LIMITED"
    assert any(row.field == "management.input_level" for row in result.requests_for_input)
    assert all(row.stability.label == "unavailable" for row in result.results)


@pytest.mark.parametrize("field", ["field_id", "as_of_date", "region"])
def test_baseline_bad_snapshot_context_and_foreign_region_abstain(field):
    c = case()
    setattr(c.snapshot, field, {"field_id": "wrong", "as_of_date": date(2026, 8, 31),
                              "region": "PL-14"}[field])
    result = recommend_baseline(c.request, c.snapshot, c.rules)
    assert not result.results and result.mode == "ABSTAIN"


def test_economics_without_yield_never_becomes_full_or_changes_baseline_order():
    c = case()
    initial = recommend_baseline(c.request, c.snapshot, c.rules)
    c.request.economics = EconomicsInput.model_validate({
        "costs_per_ha": {crop.id: {"value": 12345} for crop in c.rules.catalog},
        "contract_prices": [{"crop": crop.id, "price": 1000000 * (i + 1)}
                            for i, crop in enumerate(c.rules.catalog)],
    })
    changed = recommend_baseline(c.request, c.snapshot, c.rules)
    assert projection(initial) == projection(changed)
    assert changed.mode == "LIMITED"
    assert all(row.economics.price and row.economics.yield_t_ha is None
               and row.economics.revenue_uah_ha is None
               and row.economics.margin_uah_ha is None
               and not row.economics.complete and not row.economics.applied_to_rank
               for row in changed.results)


def test_deterministic_offline_backtest_and_ablation_leave_inputs_unchanged():
    c = case()
    before = c.model_dump_json()
    for profile in ("validated", "gaez_baseline"):
        report = backtest([c], profile)
        assert report == backtest([c], profile)
        assert report["case_count"] == 1 and report["agronomic_accuracy"] is None
        assert report["expert_review"] == "not_performed"
        ablation = ablate([c], profile)
        assert ablation == ablate([c], profile)
        assert len(ablation["comparisons"]) == 3
        assert ablation["runs"][0]["variants"]["C1"]["ranking"] == ablation["runs"][0]["variants"]["C2"]["ranking"]
        assert all(row["expert_better_fraction"] is None
                   for row in ablation["transitions"].values())
    assert c.model_dump_json() == before


@pytest.mark.parametrize("kind", ["gaez", "weather", "satellite", "history", "forecast", "historical"])
def test_backtest_audit_fails_closed_on_future_and_unavailable_archives(kind):
    c = case()
    future = datetime(2026, 9, 2, tzinfo=timezone.utc)
    if kind == "gaez":
        next(iter(c.snapshot.gaez.values()))[0].available_at = future
    elif kind in {"weather", "satellite"}:
        getattr(c.snapshot, kind).available_at = future
    elif kind == "history":
        c.snapshot.history.records[0].available_at = future
    elif kind == "forecast":
        c.snapshot.weather.provenance = [{"forecast_records_available": 1}]
    else:
        c.request.as_of_date = c.snapshot.as_of_date = date(2019, 9, 1)
    assert audit_cutoff(c)
    with pytest.raises(ValueError, match=c.case_id):
        backtest([c], "validated")


def test_blind_export_hides_variant_labels_and_explanations_and_keeps_judgments_empty():
    c = case()
    package, key = blind_package([c], "validated", "fixed-seed")
    assert (package, key) == blind_package([c], "validated", "fixed-seed")
    assert len(package["reviews"]) == len(key["assignments"]) == 3
    for review in package["reviews"]:
        assert review["review_id"] in key["assignments"]
        assert review["assessment"]["preferred"] is None
        assert review["assessment"]["assessor_id"] is None
        assert review["assessment"]["crop_acceptability"] == []
        assert review["input"]["field"]["field_id"] == review["review_id"]
        assert "C0" not in json.dumps(review) and "C3" not in json.dumps(review)
        assert "reasons" not in review and "trace" not in review


def test_diagnostics_empty_inputs_do_not_invent_performance_and_duplicate_ids_fail():
    assert backtest([], "validated")["case_count"] == 0
    report = ablate([], "validated")
    assert all(row["top3_changed_fraction"] is None for row in report["transitions"].values())
    with pytest.raises(ValueError, match="DUPLICATE_CASE_ID"):
        backtest([case(), case()], "validated")


def test_offline_cli_writes_reviewable_artifacts_and_protects_assignment_key(tmp_path):
    target, key = tmp_path / "review.json", tmp_path / "private.json"
    cli = [sys.executable, "-m", "app.validation", "blind", str(FIXTURE),
           "--profile", "validated", "--output", str(target)]
    missing_key = subprocess.run(cli, capture_output=True, text=True)
    assert missing_key.returncode == 2 and not target.exists()
    success = subprocess.run([*cli, "--key-output", str(key)], capture_output=True, text=True)
    assert success.returncode == 0, success.stderr
    assert len(json.loads(target.read_text())["reviews"]) == 3
    assert len(json.loads(key.read_text())["assignments"]) == 3
    same_path = subprocess.run([*cli, "--key-output", str(target)], capture_output=True, text=True)
    assert same_path.returncode == 2


def test_nationwide_api_local_raster_source_versions_and_enriched_replay(tmp_path, monkeypatch):
    """Non-demo path: actual raster I/O, HTTP source ingress and immutable replay."""
    from fastapi.testclient import TestClient
    from test_gaez import make_store

    import app.api.routes as routes
    from app.config import Settings
    from app.main import create_app
    from app.service import RecommendationService

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 1, 12, tzinfo=timezone.utc)

    monkeypatch.setattr(routes, "datetime", FrozenClock)
    make_store(tmp_path, available_at="2026-08-01T00:00:00Z")
    boundary = tmp_path / "regions.geojson"
    boundary.write_text(json.dumps({"type": "FeatureCollection", "features": [{
        "type": "Feature", "properties": {"region": "UA-32"}, "geometry": {
            "type": "Polygon", "coordinates": [[[34, 49], [36, 49], [36, 50],
                                                     [34, 50], [34, 49]]]}}]}))
    service = RecommendationService(Settings(
        database_url=f"sqlite:///{tmp_path / 'integration.db'}", demo=False,
        ranking_profile="gaez_baseline", gaez_root=tmp_path,
        gaez_manifest=tmp_path / "manifest.json", regions_path=boundary,
        prices_path=None, exchange_rates_path=None, satellite_path=None,
        weather_path=None, weather_enabled=False,
    ))
    payload = case().request.model_dump(mode="json", by_alias=True)
    field = payload["field"]["field_id"]
    with TestClient(create_app(service=service)) as client:
        history = client.put(f"/v1/fields/{field}/history", json=[{"crop": "soybean", "year": 2025}])
        assert history.status_code == 200, history.text
        assert history.json()["data"][0]["season_year"] == 2025
        costs = client.put(f"/v1/fields/{field}/costs", json={"wheat_spring": {"value": 12345}})
        assert costs.status_code == 200, costs.text
        price = client.post("/v1/prices/import", json={
            "csv": "crop,price\nwheat_spring,8500\n", "currency": "UAH", "unit": "t",
            "price_date": "2026-08-31", "published_at": "2026-08-31T08:00:00Z",
            "incoterm": "EXW", "vat_included": False, "source": "synthetic-integration", "region": "UA",
        })
        assert price.status_code == 201, price.text
        first_response = client.post("/v1/recommendations", json=payload)
        assert first_response.status_code == 200, first_response.text
        first = first_response.json()
        assert first["mode"] == "LIMITED" and not first["provenance"]["demo"]
        assert first["provenance"]["algorithm_version"].startswith("gaez-baseline")
        assert len(first["results"]) == 1
        row = first["results"][0]
        assert row["crop_variant"] == "wheat_spring" and row["status"] == "conditional"
        assert row["economics"]["price"]["base"] == 8500
        assert row["economics"]["costs_uah_ha"] == 12345
        assert all(row["economics"][key] is None
                   for key in ("yield_t_ha", "revenue_uah_ha", "margin_uah_ha"))
        assert not row["economics"]["applied_to_rank"]
        trace = {event["id"] for event in first["trace"]}
        assert row["reasons"] and row["limitations"]
        assert all(item["trace_event_id"] in trace
                   for item in row["reasons"] + row["limitations"])
        snapshot = service.storage.get_snapshot(first["provenance"]["snapshot_id"])
        assert snapshot.region == "UA-32"
        assert snapshot.history.records[0].crop == "soybean"
        assert snapshot.costs_per_ha["wheat_spring"].value == 12345
        assert snapshot.prices[0].price_value == 8500
        # A newer source version after T must neither invalidate the T cache nor
        # overwrite the old evidence used for that request.
        for kind, source in (("history", history.json()), ("costs", costs.json()), ("prices", price.json())):
            future = dict(source)
            future["available_at"] = "2026-09-02T00:00:00Z"
            future["data"] = [] if kind != "costs" else {"wheat_spring": {"value": 999999}}
            service.storage.put_source(kind, "*" if kind == "prices" else field, future)
        def forbidden_prepare(*args, **kwargs):
            raise AssertionError("saved snapshot must be reused without source collection")
        monkeypatch.setattr(service.preparer, "prepare", forbidden_prepare)
        assert client.post("/v1/recommendations", json=payload).json() == first
        assert client.post("/v1/recommendations", json=payload,
                           params={"snapshot_id": snapshot.snapshot_id}).json() == first
        assert client.get(f"/v1/recommendations/{first['recommendation_id']}").json() == first
        # Old crop-year and price dates do not backdate a newly ingested source.
        monkeypatch.undo()
        past = dict(payload, as_of_date="2026-08-31")
        historical = client.post("/v1/recommendations", json=past)
        assert historical.status_code == 200, historical.text
        older = service.storage.get_snapshot(historical.json()["provenance"]["snapshot_id"])
        assert not older.history.records and not older.costs_per_ha and not older.prices
        # One unavailable optional source is preserved as a partial job and an
        # explicit source error; it must not erase the usable local GAEZ layer.
        service.settings.weather_path = tmp_path / "missing-weather.json"
        queued = client.post(f"/v1/fields/{field}/prepare", json=payload)
        assert queued.status_code == 202
        assert service.process_one_job()
        job = client.get(queued.headers["location"]).json()
        assert job["status"] == "partial"
        partial = service.storage.get_snapshot(job["snapshot_id"])
        assert any(error.source == "weather" and error.code == "IMPORT_ERROR"
                   for error in partial.source_errors)
        limited = client.post("/v1/recommendations", json=payload).json()
        assert limited["mode"] == "LIMITED" and limited["results"]
        assert any(error["source"] == "weather" for error in limited["provenance"]["source_errors"])
