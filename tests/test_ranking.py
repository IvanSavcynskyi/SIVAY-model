from datetime import date, datetime, timezone
from itertools import product

import pytest

from app.domain.models import (
    CellValue, CropHistoryRecord, CropVariant, FieldFeatureSnapshot, FieldInput,
    EconomicsResult, GaezFieldValue, Geometry, Interval, NormalizedHistory,
    RecommendationRequest, RotationRule, RuleCondition, RuleTarget, ScenarioValues,
    RotationResult, SowingWindow, TargetSeason, WeatherFeatures, WeatherResult,
)
from app.ranking import recommend
from app.ranking.engine import Evaluation, _ordered, build_tiers, gaez_interval
from app.rules.loader import load_rules

NOW = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def request(**kwargs):
    return RecommendationRequest(
        field=FieldInput(field_id="field-1", crs="EPSG:4326", geometry=Geometry(
            type="Polygon", coordinates=[[[33, 49], [33.01, 49], [33.01, 49.01], [33, 49]]])),
        as_of_date=date(2026, 9, 1),
        target_season=TargetSeason(harvest_year=2027, include=["winter", "spring"]),
        **kwargs,
    )


def layer(code="WHE", value=7800, **kwargs):
    data = dict(layer_code=f"RES05-SUX.{code}", variable="RES05-SUX", unit="index",
        crop_code=code, native_res_deg=5/60, field_area_ha=50, cell_area_ha_mean=5000,
        field_to_cell_ratio=.01, coverage_valid=1, n_cells=1, mean_w=value,
        source_sha256="a"*64, retrieved_at=NOW, available_at=NOW)
    data.update(kwargs)
    return GaezFieldValue(**data)


def snapshot(**kwargs):
    data = dict(snapshot_id="snap-1", field_id="field-1", geom_hash="geom",
        as_of_date=NOW.date(), prepared_at=NOW, region="UA-53", request_hash="req",
        demo=True, gaez={"WHE": [layer()]}, history=NormalizedHistory(
            records=[CropHistoryRecord(season_year=2026, crop="pea", reliability="high", status="harvested")], quality="high", occupied=False))
    data.update(kwargs)
    return FieldFeatureSnapshot(**data)


def rules(crops=None):
    bundle = load_rules("rules/demo", allow_demo=True)
    crops = crops or [next(c for c in bundle.catalog if c.id == "wheat_winter")]
    return bundle.model_copy(update={"catalog": crops, "rotation": []})


def test_pure_deterministic_no_mutation_and_hash_changes():
    req, snap, rule_set = request(), snapshot(), rules()
    before = (req.model_dump_json(), snap.model_dump_json(), rule_set.model_dump_json())
    first = recommend(req, snap, rule_set)
    assert first.model_dump_json() == recommend(req, snap, rule_set).model_dump_json()
    assert first.computed_at == snap.prepared_at
    assert before == (req.model_dump_json(), snap.model_dump_json(), rule_set.model_dump_json())
    assert recommend(req, snap, rule_set.model_copy(update={"version": "next"})).recommendation_id != first.recommendation_id
    assert first.results[0].stability.scenarios_evaluated == 12


def test_production_drafts_abstain_and_demo_requires_both_flags():
    production = load_rules("rules")
    result = recommend(request(), snapshot(demo=False), production)
    assert result.mode == "ABSTAIN" and not result.results
    assert recommend(request(), snapshot(demo=False), rules()).mode == "ABSTAIN"
    assert recommend(request(), snapshot(), rules()).results


def test_parent_crop_constraint_excludes_variants():
    req = request(constraints={"excluded_crops": ["wheat"]})
    result = recommend(req, snapshot(), rules())
    assert [(c.crop_variant, c.gate) for c in result.excluded] == [("wheat_winter", "G1")]


def test_closed_window_excludes_without_gaez():
    req = request().model_copy(update={"as_of_date": date(2026, 12, 1)})
    result = recommend(req, snapshot(as_of_date=req.as_of_date, gaez={}), rules())
    assert result.excluded[0].gate == "G2"


@pytest.mark.parametrize("start,end,expected", [
    ("2026-11-01", "2026-11-10", "excluded"),
    ("2026-10-01", "2026-11-10", "conditional"),
    ("2026-09-01", "2026-09-10", "recommended"),
])
def test_harvest_plan_range_uses_earliest_for_ban_latest_for_condition(start, end, expected):
    current = CropHistoryRecord(season_year=2026, crop="sunflower", status="in_field", reliability="high",
        harvest_date_planned={"from": start, "to": end})
    result = recommend(request(), snapshot(history=NormalizedHistory(records=[current], current_crop=current, quality="high")), rules())
    if expected == "excluded":
        assert result.excluded[0].gate == "G2"
    else:
        # Only conditional candidates cannot fill a recommendation list. They
        # ask for the information needed to unblock the field instead.
        if expected == "conditional":
            assert result.mode == "NEEDS_INPUT" and result.requests_for_input
        else:
            assert result.results[0].status == expected


def test_g3_only_reliable_history_and_active_rule_can_exclude():
    rule = RotationRule(id="ban", version=1, status="demo", regions=["UA-53"],
        applies_to=RuleTarget(candidate="wheat_winter"), condition=RuleCondition(type="same_crop"),
        effect="incompatible", explanation_uk="Несумісність")
    rs = rules().model_copy(update={"rotation": [rule]})
    row = CropHistoryRecord(season_year=2026, crop="wheat", status="harvested", reliability="high")
    snap = snapshot(history=NormalizedHistory(records=[row], quality="high", occupied=False))
    assert recommend(request(), snap, rs).excluded[0].gate == "G3"
    row.reliability = "low"
    assert not recommend(request(), snap, rs).excluded
    rule.status = "draft"
    assert recommend(request(), snap, rs).results


def test_g4_requires_both_resolutions_and_periods_and_respects_farm_success():
    scx = [layer(variable="SCX", layer_code=f"SCX.{period}.{fine}", period=period,
        native_res_deg=1/120 if fine else 5/60, class_shares={8: .95})
        for period, fine in product(("HP0120", "FP2140"), (False, True))]
    assert not recommend(request(), snapshot(gaez={"WHE": [layer(), *scx[:1]]}), rules()).excluded
    assert recommend(request(), snapshot(gaez={"WHE": [layer(), *scx]}), rules()).excluded[0].gate == "G4"
    success = CropHistoryRecord(season_year=2025, crop="wheat", status="harvested", reliability="high", yield_t_ha=4)
    snap = snapshot(gaez={"WHE": [layer(), *scx]}, history=NormalizedHistory(records=[success], quality="high", occupied=False))
    assert recommend(request(), snap, rules()).results


def test_g5_no_coverage_is_not_evaluated_and_not_zero_score_recommendation():
    result = recommend(request(), snapshot(gaez={"WHE": [layer(coverage_valid=.49)]}), rules())
    assert result.mode == "ABSTAIN" and not result.excluded
    assert result.not_evaluated[0].reason == "GAEZ_NO_COVERAGE"


def test_gaez_interval_cell_fine_future_and_partial_coverage():
    base = layer(per_cell=[CellValue(value=7100, area_fraction=.7), CellValue(value=8000, area_fraction=.3)])
    fine = layer(variable="SXX", native_res_deg=1/120, value=7300)
    future = layer(period="FP2140", clim="ENSEMBLE", ssp="SSP370", value=7600)
    score, _, _ = gaez_interval([base, fine, future], "HRLM")
    assert score == Interval(lo=.71, mid=.78, hi=.8)
    partial = base.model_copy(update={"coverage_valid": .7})
    assert gaez_interval([partial], "HRLM")[0].lo == 0


def test_gaez_sxx_fallback_not_full_suitability():
    score, base, issues = gaez_interval([layer(variable="SXX", value=7000)], "HRLM")
    assert score.lo == pytest.approx(.56) and score.mid == .7
    assert "gaez_fallback_all_land" in issues


def test_future_publication_does_not_enter_gaez_interval():
    unavailable = layer(value=10000, available_at=datetime(2026, 9, 2, tzinfo=timezone.utc))
    result = recommend(request(), snapshot(gaez={"WHE": [unavailable]}), rules())
    assert not result.results and not result.provenance.gaez_layers


def test_missing_history_and_weather_never_raise_conservative_score():
    for rotation_effect in ("neutral", "favorable", "discouraged_strong"):
        rs = rules()
        rr = RotationRule(id="effect", version=1, status="demo", regions=["UA-53"],
            applies_to=RuleTarget(candidate="wheat_winter"), condition=RuleCondition(type="predecessor_is", predecessor="pea"),
            effect=rotation_effect, explanation_uk="Попередник")
        rs.rotation = [rr]
        for signal in ("dry", "normal", "wet_ok"):
            snap = snapshot(weather=WeatherFeatures(signal=signal, quality="high", available_at=NOW))
            known = recommend(request(), snap, rs).results[0].agro_score.lo
            unknown = recommend(request(), snap.model_copy(update={"history": NormalizedHistory(occupied=False), "weather": WeatherFeatures()}), rs).results[0].agro_score.lo
            assert unknown <= known


def test_satellite_never_changes_rank_or_agro_score():
    snap = snapshot()
    before = recommend(request(), snap, rules())
    snap.satellite.quality = "high"
    snap.satellite.occupied = True
    snap.satellite.obs_count_valid = 100
    after = recommend(request(), snap, rules())
    assert [(r.crop_variant, r.agro_score) for r in before.results] == [(r.crop_variant, r.agro_score) for r in after.results]


def test_summer_candidates_included_by_spring_season():
    rs = rules(crops=[CropVariant(id="maize_grain", name_uk="Кукурудза", family="cereals", season_type="summer", gaez_code="MZE", status="demo")])
    rs.sowing_windows = [SowingWindow(crop_variant="maize_grain", region="UA-53", earliest="04-01", latest="06-01", prep_days=7, min_lead_days=7, status="demo")]
    assert recommend(request(), snapshot(gaez={"MZE": [layer(code="MZE")]}), rs).results[0].crop_variant == "maize_grain"


def test_design_section8_golden_agro_arithmetic_with_conservative_order():
    # Numeric agronomy matches §8. Ranking intentionally follows ADR conservative
    # bounds; the contradictory economic-slot order is not a supported oracle.
    crops = []
    layers = {}
    values = [("maize_grain", "MZE", .66, .74, .77), ("barley_spring", "BRL", .66, .72, .75),
              ("sugar_beet", "SUB", .60, .70, .73), ("pea_dry", "PEA", .62, .68, .71)]
    for identifier, code, lo, mid, hi in values:
        crops.append(CropVariant(id=identifier, name_uk=identifier, family="test", season_type="spring", gaez_code=code, status="demo"))
        layers[code] = [layer(code=code, value=mid*10000, per_cell=[CellValue(value=lo*10000, area_fraction=.5), CellValue(value=hi*10000, area_fraction=.5)])]
    rs = rules(crops)
    rs.sowing_windows = [SowingWindow(crop_variant=c.id, region="UA-53", earliest="03-01", latest="06-01", prep_days=7, min_lead_days=7, status="demo") for c in crops]
    result = recommend(request(), snapshot(gaez=layers), rs)
    assert [r.crop_variant for r in result.results] == ["maize_grain", "barley_spring", "pea_dry", "sugar_beet"]
    assert [(r.agro_score.lo, r.agro_score.mid, r.agro_score.hi) for r in result.results] == [(lo, mid, hi) for _, _, lo, mid, hi in [values[0], values[1], values[3], values[2]]]


def evaluation(identifier, lo, mid, hi, margin=None):
    score = Interval(lo=lo, mid=mid, hi=hi)
    crop = CropVariant(id=identifier, name_uk=identifier, family="test", season_type="spring", status="demo")
    eco = EconomicsResult(complete=True, margin_uah_ha=ScenarioValues(low=margin-10, base=margin, high=margin+10)) if margin is not None else None
    return Evaluation(crop=crop, gaez=score, layer=layer(), rotation=RotationResult(multiplier=Interval(lo=1, mid=1, hi=1)),
        weather=WeatherResult(multiplier=Interval(lo=1, mid=1, hi=1)), score=score, economics=eco, issues=[], event_id="event")


def test_contiguous_tiers_do_not_promote_a_wide_lower_bound():
    a = evaluation("a", .8, .85, .9)
    b = evaluation("b", .6, .65, .7)
    c = evaluation("c", .5, .7, .95, margin=999999)
    assert [[e.crop.id for e in tier] for tier in build_tiers([c, b, a])] == [["a"], ["b", "c"]]
    assert [e.crop.id for e, _ in _ordered([c, b, a])] == ["a", "b", "c"]


def test_economics_only_exact_agro_ties_and_removing_it_never_improves_own_place():
    a = evaluation("a", .6, .7, .8, margin=100)
    b = evaluation("b", .6, .65, .8, margin=200)
    c = evaluation("c", .59, .7, .8, margin=100000)
    assert [e.crop.id for e, _ in _ordered([a, b, c])] == ["b", "a", "c"]
    for candidate in (a, b, c):
        before = [e.crop.id for e, _ in _ordered([a, b, c])].index(candidate.crop.id)
        original = candidate.economics
        candidate.economics = None
        after = [e.crop.id for e, _ in _ordered([a, b, c])].index(candidate.crop.id)
        candidate.economics = original
        assert after >= before


def test_lowering_own_conservative_bound_cannot_improve_order_property_grid():
    from dataclasses import replace
    for lows in product((.3, .5, .7), repeat=3):
        candidates = [evaluation(str(i), lo, max(.75, lo), .9) for i, lo in enumerate(lows)]
        for candidate in candidates:
            before = [e.crop.id for e, _ in _ordered(candidates)].index(candidate.crop.id)
            widened = replace(candidate, score=Interval(lo=max(0, candidate.score.lo-.2), mid=candidate.score.mid, hi=1))
            altered = [widened if e.crop.id == candidate.crop.id else e for e in candidates]
            after = [e.crop.id for e, _ in _ordered(altered)].index(candidate.crop.id)
            assert after >= before


def test_unknown_management_without_alternate_layers_cannot_claim_stability():
    snap = snapshot(assumptions=["input_level=high (assumed)"])
    result = recommend(request(), snap, rules())
    assert result.mode == "NEEDS_INPUT"
    assert result.results[0].stability.label == "unavailable"
    assert any(item.field == "management.input_level" for item in result.requests_for_input)


def test_unknown_management_with_alternate_layers_is_recomputed():
    snap = snapshot(assumptions=["input_level=high (assumed)"], gaez={"WHE": [layer(), layer(wsim="LRLM", value=3000)]})
    result = recommend(request(), snap, rules())
    assert result.mode == "NEEDS_INPUT"
    assert result.results[0].stability.scenarios_evaluated == 24
    assert result.results[0].stability.p_top5 == .5


def test_missing_winter_harvest_does_not_assume_free_field():
    snap = snapshot(history=NormalizedHistory())
    result = recommend(request(), snap, rules())
    assert result.mode == "NEEDS_INPUT" and not result.results
    assert any(item.field == "crop_history.harvest_date_planned" for item in result.requests_for_input)


def test_snapshot_context_mismatch_abstains():
    assert recommend(request(), snapshot(field_id="other"), rules()).mode == "ABSTAIN"
