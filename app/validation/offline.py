"""Diagnostics over sealed snapshots, with no source collection or network access.

The ablation interventions are engineering experiments, never approved rules.
They must not be served as recommendations. No yields or expert judgments are
inferred, and agreement with a farmer's choice is not treated as accuracy.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations
from statistics import mean
from typing import Callable, Literal

from pydantic import Field

from app.canonical import digest
from app.domain.models import (
    FieldFeatureSnapshot, Interval, Model, RecommendationRequest,
    RecommendationResult, RuleSet, SatelliteFeatures, as_of_cutoff,
)

Profile = Literal["gaez_baseline", "validated"]
Ranker = Callable[[RecommendationRequest, FieldFeatureSnapshot, RuleSet], RecommendationResult]


class ValidationCase(Model):
    case_id: str = Field(min_length=1)
    request: RecommendationRequest
    snapshot: FieldFeatureSnapshot
    rules: RuleSet


def _ranker(profile: Profile) -> Ranker:
    if profile == "validated":
        from app.ranking.engine import recommend
        return recommend
    if profile == "gaez_baseline":
        from app.ranking.baseline import recommend_baseline
        return recommend_baseline
    raise ValueError(f"Unknown profile: {profile}")


def audit_cutoff(case: ValidationCase) -> list[str]:
    """Fail closed on unsealed temporal inputs rather than silently backdating.

    Existing rankers use HP0120; there is no supported HP8100 scoring profile.
    Historical runs through 2020 are therefore explicitly unavailable. Archive
    forecast issuance cannot be reconstructed from a WeatherFeatures snapshot;
    backtests containing forecast records must supply a separate archive audit.
    """
    req, snap = case.request, case.snapshot
    cutoff = as_of_cutoff(req.as_of_date)
    issues = []
    if snap.field_id != req.field.field_id or snap.as_of_date != req.as_of_date:
        issues.append("SNAPSHOT_CONTEXT_MISMATCH")
    if req.as_of_date.year <= 2020:
        issues.append("HISTORICAL_GAEZ_PROFILE_UNAVAILABLE")
    for code, layers in sorted(snap.gaez.items()):
        for layer in layers:
            if layer.available_at > cutoff:
                issues.append(f"GAEZ_UNAVAILABLE:{code}:{layer.layer_code}")
    for name in ("weather", "satellite"):
        feature = getattr(snap, name)
        if feature.available_at is not None and feature.available_at > cutoff:
            issues.append(f"{name.upper()}_UNAVAILABLE")
    for row in snap.weather.provenance:
        if row.get("forecast_records_available", 0):
            issues.append("FORECAST_ARCHIVE_AUDIT_REQUIRED")
    for row in snap.prices:
        if (row.published_at > cutoff or (row.available_at or row.published_at) > cutoff
                or row.price_date > req.as_of_date):
            issues.append(f"PRICE_UNAVAILABLE:{row.price_id}")
    for row in snap.exchange_rates:
        if (row.available_at is None or row.available_at > cutoff
                or row.date > req.as_of_date):
            issues.append(f"FX_UNAVAILABLE:{row.currency}:{row.date}")
    for row in snap.history.records:
        if row.available_at is not None and row.available_at > cutoff:
            issues.append(f"HISTORY_UNAVAILABLE:{row.record_id or row.season_year}")
        if row.harvest_date and row.harvest_date > req.as_of_date:
            issues.append(f"FUTURE_ACTUAL_HARVEST:{row.record_id or row.season_year}")
        if row.sowing_date and row.sowing_date > req.as_of_date:
            issues.append(f"FUTURE_ACTUAL_SOWING:{row.record_id or row.season_year}")
    if req.economics:
        for row in req.economics.contract_prices:
            if row.available_at is not None and row.available_at > cutoff:
                issues.append(f"CONTRACT_UNAVAILABLE:{row.crop}")
    return sorted(set(issues))


def _validate(cases: list[ValidationCase]) -> None:
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("DUPLICATE_CASE_ID")
    for case in cases:
        issues = audit_cutoff(case)
        if issues:
            raise ValueError(f"{case.case_id}: {', '.join(issues)}")


def _summary(result: RecommendationResult) -> dict:
    return {
        "recommendation_id": result.recommendation_id,
        "mode": result.mode,
        "ranking": [row.crop_variant for row in result.results],
        "stability": {row.crop_variant: row.stability.model_dump(mode="json")
                      for row in result.results},
        "not_evaluated": [row.model_dump(mode="json") for row in result.not_evaluated],
    }


def backtest(cases: list[ValidationCase], profile: Profile = "gaez_baseline") -> dict:
    """Replay snapshots and report technical outcomes, without outcome metrics."""
    _validate(cases)
    rank = _ranker(profile)
    runs = [{"case_id": case.case_id, **_summary(rank(case.request, case.snapshot, case.rules))}
            for case in cases]
    return {
        "kind": "offline_snapshot_replay", "profile": profile,
        "dataset_sha256": digest([case.model_dump(mode="json") for case in cases]),
        "case_count": len(cases),
        "mode_counts": dict(sorted(Counter(run["mode"] for run in runs).items())),
        "agronomic_accuracy": None, "expert_review": "not_performed",
        "limitations": ["No observed outcomes or counterfactual yields are inferred.",
                        "Historical GAEZ runs through 2020 are unsupported.",
                        "Forecast snapshots require archived issuance audit."],
        "runs": runs,
    }


def _intervention(case: ValidationCase, level: int) -> ValidationCase:
    changed = case.model_copy(deep=True)
    if level < 3:
        changed.request.economics = None
        changed.snapshot.prices = []
        changed.snapshot.exchange_rates = []
        changed.snapshot.costs_per_ha = {}
    if level < 2:
        changed.snapshot.satellite = SatelliteFeatures()
    if level == 0:
        one = Interval(lo=1, mid=1, hi=1)
        changed.rules.parameters.weather_missing = one
        changed.rules.parameters.weather_multipliers = {
            name: one.model_copy() for name in ("dry", "normal", "wet_ok")}
    # Include diagnostic interventions in deterministic experiment provenance.
    changed.rules.version += f":diagnostic-C{level}"
    changed.snapshot.snapshot_id = digest({"source": case.snapshot.snapshot_id,
                                          "case": case.case_id, "level": level})
    return changed


def _comparison(left: list[str], right: list[str]) -> dict:
    common = set(left) & set(right)
    pairs = list(combinations(sorted(common), 2))
    left_pos, right_pos = {c: i for i, c in enumerate(left)}, {c: i for i, c in enumerate(right)}
    discordant = sum((left_pos[a] < left_pos[b]) != (right_pos[a] < right_pos[b])
                     for a, b in pairs)
    return {"top3_changed": left[:3] != right[:3],
            "common_candidates": len(common),
            "kendall_tau_common": 1 - 2 * discordant / len(pairs) if pairs else None,
            "top5_jaccard": len(common) / len(set(left) | set(right))
            if left or right else None}


def ablate(cases: list[ValidationCase], profile: Profile = "gaez_baseline") -> dict:
    """Compare C0→C3 on exactly the same sealed fields and deterministic inputs."""
    _validate(cases)
    rank = _ranker(profile)
    runs, comparisons = [], []
    for case in cases:
        variants = {}
        for level in range(4):
            changed = _intervention(case, level)
            variants[f"C{level}"] = _summary(rank(changed.request, changed.snapshot, changed.rules))
        runs.append({"case_id": case.case_id, "variants": variants})
        for level in range(3):
            before, after = f"C{level}", f"C{level+1}"
            comparisons.append({"case_id": case.case_id, "transition": f"{before}->{after}",
                                **_comparison(variants[before]["ranking"], variants[after]["ranking"])})
    transitions = {}
    for transition in ("C0->C1", "C1->C2", "C2->C3"):
        rows = [row for row in comparisons if row["transition"] == transition]
        taus = [row["kendall_tau_common"] for row in rows if row["kendall_tau_common"] is not None]
        transitions[transition] = {
            "field_count": len(rows),
            "top3_changed_fraction": mean(row["top3_changed"] for row in rows) if rows else None,
            "mean_kendall_tau_common": mean(taus) if taus else None,
            "expert_better_fraction": None, "expert_worse_fraction": None,
            "expert_equal_fraction": None,
        }
    return {
        "kind": "diagnostic_ablation", "profile": profile,
        "dataset_sha256": digest([case.model_dump(mode="json") for case in cases]),
        "case_count": len(cases), "transitions": transitions,
        "comparisons": comparisons, "runs": runs,
        "limitations": ["Diagnostic neutralization is not an approved agronomic ruleset.",
                        "No approved satellite classifier: C1 and C2 have identical ranking.",
                        "Kendall tau uses common candidates only; additions/removals use Jaccard.",
                        "Weather and economics do not affect the GAEZ baseline profile.",
                        "No expert judgments or observed outcomes are available."],
    }


def blind_package(cases: list[ValidationCase], profile: Profile = "gaez_baseline",
                  seed: str = "sivay-review-v1") -> tuple[dict, dict]:
    """Export paired lists without reasons, scores, source labels, or outcomes.

    Keep the returned assignment key separate from the reviewer package. Stable
    hash ordering makes left/right assignment reproducible without randomness.
    """
    report = ablate(cases, profile)
    source = {case.case_id: case for case in cases}
    reviews, key = [], {}
    for run in report["runs"]:
        case = source[run["case_id"]]
        for level in range(3):
            token = digest({"seed": seed, "case": case.case_id, "level": level})
            pair = [f"C{level}", f"C{level+1}"]
            if int(token[-1], 16) % 2:
                pair.reverse()
            context = case.request.model_dump(mode="json", by_alias=True)
            # IDs are replaced by the review token; omit machine explanations.
            context["field"]["field_id"] = token[:16]
            reviews.append({"review_id": token[:16], "input": context,
                            "list_a": run["variants"][pair[0]]["ranking"],
                            "list_b": run["variants"][pair[1]]["ranking"],
                            "assessment": {"assessor_id": None, "preferred": None,
                                           "crop_acceptability": [], "missing_crops": [],
                                           "reason": None}})
            key[token[:16]] = {"case_id": case.case_id, "list_a": pair[0], "list_b": pair[1]}
    return ({"instructions": "Assess each crop as acceptable/conditional/unacceptable; "
             "identify missing crops; choose a/b/equal. Leave unknown judgments empty.",
             "reviews": reviews}, {"profile": profile, "dataset_sha256": report["dataset_sha256"],
                                    "assignments": key})
