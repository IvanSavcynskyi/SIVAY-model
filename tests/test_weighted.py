from copy import deepcopy

import pytest
from hypothesis import given, strategies as st
from pydantic import ValidationError

from app.domain import Interval, PersonalizationAssessment, PersonalizationPolicy, PersonalizationRule
from app.ranking.weighted import calculate_weighted
from app.ranking.personalized import recommend_personalized
from test_personalization import policy, synthetic_candidates
from test_ranking import request


def rule(identifier="water", **updates):
    data = dict(id=identifier, crops=["*"], metric="weather.precip_7d", operator="lt",
                threshold=10, full_risk_at=0, weight=.2, source="synthetic", explanation_uk="Перевірка.")
    data.update(updates)
    return PersonalizationRule(**data)


def assessment(identifier="water", outcome="risk", value=5):
    return PersonalizationAssessment(rule_id=identifier, source="synthetic", outcome=outcome,
        explanation_uk="Перевірка.", facts={"value": value})


def calculate(rules, assessments, base=.8):
    configured = PersonalizationPolicy(version="synthetic", source="synthetic", rules=rules)
    return calculate_weighted(Interval(lo=base, mid=base, hi=base), assessments, configured)


def test_exact_grouped_multiplication_and_linear_severity():
    water = rule()
    frost = rule("frost", metric="forecast.tmin_7d", threshold=0, full_risk_at=-2, weight=.3)
    result = calculate([water, frost], [assessment(), assessment("frost", value=-1)])
    assert result.multiplier.mid == pytest.approx(.9*.85)
    assert result.final_score.mid == pytest.approx(.8*.9*.85)
    assert result.complete
    assert {c.group for c in result.contributions} == {"moisture", "temperature"}


def test_correlated_signals_and_duplicate_rules_do_not_compound_water_penalty():
    first = rule()
    second = rule("ndmi", metric="NDMI.mean", threshold=.2, full_risk_at=-.2, weight=.3)
    result = calculate([first, second], [assessment(), assessment("ndmi", value=0)])
    assert len(result.groups) == 1
    assert result.groups[0].penalty.mid == pytest.approx(.15)
    assert result.final_score.mid == pytest.approx(.8*.85)
    duplicate = second.model_copy(update={"id": "duplicate"})
    duplicated = calculate([first, second, duplicate], [assessment(), assessment("ndmi", value=0), assessment("duplicate", value=0)])
    assert duplicated.final_score == result.final_score


@pytest.mark.parametrize("operator,threshold,critical,value", [("lt", 10, 0, -5), ("gt", 20, 30, 40)])
def test_penalties_saturate_at_configured_maximum(operator, threshold, critical, value):
    configured = rule(operator=operator, threshold=threshold, full_risk_at=critical, weight=.2)
    result = calculate([configured], [assessment(value=value)])
    assert result.contributions[0].severity.mid == 1
    assert result.final_score.mid == pytest.approx(.64)


def test_missing_metric_widens_interval_without_improving_ranking_endpoints():
    known = calculate([rule()], [assessment(value=7)])
    missing = calculate([rule()], [assessment(outcome="unknown", value=None)])
    assert missing.final_score.lo <= known.final_score.lo
    assert missing.final_score.mid <= known.final_score.mid
    assert missing.final_score.hi == .8
    assert not missing.complete


def test_zero_weight_and_not_applicable_rules_are_neutral():
    result = calculate([rule(weight=0)], [assessment(outcome="unknown", value=None)])
    assert result.complete and result.final_score.mid == .8
    assert calculate([rule()], [assessment(outcome="not_applicable", value=None)]).final_score.mid == .8


@given(st.floats(min_value=0, max_value=1, allow_nan=False),
       st.floats(min_value=0, max_value=1, allow_nan=False),
       st.floats(min_value=-100, max_value=10, allow_nan=False))
def test_weighted_score_is_bounded_and_monotone(base, weight, value):
    configured = rule(weight=weight)
    before = calculate([configured], [assessment(value=value)], base)
    worse = calculate([configured], [assessment(value=value-1)], base)
    assert 0 <= worse.final_score.lo <= worse.final_score.mid <= worse.final_score.hi <= base
    assert worse.final_score.mid <= before.final_score.mid + 1e-12


def test_invalid_ramps_weights_and_reserved_ids_are_rejected():
    for updates in ({"weight": 1.1}, {"weight": -.1}, {"full_risk_at": 10}, {"operator": "gt", "full_risk_at": 0}):
        with pytest.raises(ValidationError):
            rule(**updates)
    with pytest.raises(ValidationError, match="reserved"):
        calculate([rule("rotation.hidden")], [])


def test_gradual_penalties_change_ranking_even_when_both_rules_have_same_priority():
    crops, snap, bundle = synthetic_candidates(2)
    configured = [rule("water", crops=["crop0"], weight=.5),
        rule("ndvi", crops=["crop1"], metric="NDVI.delta", threshold=-.1, full_risk_at=-.2, weight=.5)]
    snap.field_signals.policy = policy(rules=[r.model_dump(mode="json") for r in configured])
    snap.field_signals.metrics = {"weather.precip_7d": 5, "NDVI.delta": -.11}
    snap.field_signals.weather["sowing_dates"] = {c.id: "2026-09-05" for c in crops}
    original = deepcopy(snap.model_dump())
    result = recommend_personalized(request(), snap, bundle)
    assert result.results[0].crop_variant == "crop1"
    assert result.results[0].ranking_score.mid == pytest.approx(.89*.95)
    assert result.results[1].ranking_score.mid == pytest.approx(.9*.75)
    assert result.results[0].personalization.ranking_method == "rotation_then_weighted_score"
    assert original == snap.model_dump()


def test_vegetation_adjustments_are_skipped_for_bare_cover_and_unknown_without_ndvi():
    _, snap, bundle = synthetic_candidates(1)
    configured = rule("ndmi", metric="NDMI.delta", threshold=-.1, full_risk_at=-.2,
                      weight=.1, requires_vegetation=True)
    snap.field_signals.policy = policy(rules=[configured.model_dump(mode="json")])
    snap.field_signals.weather["sowing_dates"] = {"crop0": "2026-09-05"}
    snap.field_signals.metrics = {"NDMI.delta": -.3, "NDVI.mean": .1}
    bare = recommend_personalized(request(), snap, bundle).results[0]
    assert bare.ranking_score.mid == .9
    assert bare.personalization.assessments[0].outcome == "not_applicable"
    snap.field_signals.metrics["NDVI.mean"] = .6
    active = recommend_personalized(request(), snap, bundle).results[0]
    assert active.ranking_score.mid == pytest.approx(.81)
    snap.field_signals.metrics.pop("NDVI.mean")
    unknown = recommend_personalized(request(), snap, bundle).results[0]
    assert unknown.ranking_score.lo == pytest.approx(.81)
    assert unknown.ranking_score.hi == .9
    assert not unknown.personalization.weighted.complete
