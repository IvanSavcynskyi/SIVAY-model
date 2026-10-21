"""Nationwide, source-based GAEZ potential without unapproved agronomy.

This explicit MVP profile reports LIMITED, conditional potential, not a verified
planting plan. Rotation/weather/calendar adjustments and economic ranking are off.
"""
from __future__ import annotations

from statistics import median

from app.canonical import digest
from app.domain import (
    DataQualityReport, DataQualitySummary, EconomicsInput, Explanation,
    FieldFeatureSnapshot, GaezResult, InputRequest, Interval, NotEvaluatedCrop,
    Provenance, RankedCrop, RecommendationRequest, RecommendationResult,
    RotationResult, RuleSet, StabilityResult, TraceEvent, WeatherResult, as_of_cutoff,
)
from app.features.economics import compute_economics

ALGORITHM_VERSION = "gaez-baseline-1.0.0"


def recommend_baseline(req: RecommendationRequest, snap: FieldFeatureSnapshot,
                       rules: RuleSet) -> RecommendationResult:
    cutoff = as_of_cutoff(req.as_of_date)
    trace, not_evaluated, excluded, below = [], [], [], []
    def event(code, crop=None, **params):
        identifier = f"base_{len(trace)+1:04d}"
        trace.append(TraceEvent(id=identifier, code=code, crop=crop, params=params))
        return identifier
    assumptions = [*snap.assumptions,
        "GAEZ_BASELINE: календарі посіву, сівозміна та погодні поправки вимкнені без затверджених правил",
        "Локальні ґрунти, техніка й можливість своєчасного посіву потребують перевірки",
        "Економічне ранжування вимкнене; урожайність не припускається"]
    seasons = set(req.target_season.include)
    if "spring" in seasons:
        seasons.add("summer")
    wsim = ("H" if req.management.input_level == "high" else "L") + (
        "I" if req.management.irrigation == "irrigated" else "R") + "LM"
    evaluations = []
    context_ok = (snap.field_id == req.field.field_id and snap.as_of_date == req.as_of_date
                  and snap.demo == rules.demo)
    region_ok = snap.region == "UA" or bool(snap.region and snap.region.startswith("UA-"))
    if not context_ok:
        event("SNAPSHOT_CONTEXT_MISMATCH")
    elif not region_ok:
        event("REGION_NOT_SUPPORTED")
    else:
        for crop in sorted(rules.catalog, key=lambda c: c.id):
            if crop.season_type not in seasons or crop.status == "retired":
                continue
            # Calendar semantics only: winter belongs to prior calendar year.
            latest_sowing_year = req.target_season.harvest_year - (crop.season_type == "winter")
            if req.as_of_date.year > latest_sowing_year:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="TARGET_SOWING_YEAR_PASSED"))
                continue
            parent = crop.parent_crop or crop.id.removesuffix("_winter").removesuffix("_spring").removesuffix("_grain")
            if {crop.id, parent} & set(req.constraints.excluded_crops):
                from app.domain import ExcludedCrop
                ref = event("USER_CONSTRAINT", crop.id)
                excluded.append(ExcludedCrop(crop_variant=crop.id, gate="USER_CONSTRAINT",
                    text="Культуру виключено користувачем.", trace_event_id=ref))
                continue
            if crop.gaez_code is None:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="NOT_IN_GAEZ_CATALOG"))
                continue
            layers = [v for v in snap.gaez.get(crop.gaez_code, []) if v.available_at <= cutoff
                      and v.wsim == wsim and v.period == "HP0120" and v.ssp == "HIST"]
            historical = [v for v in layers if v.variable.removeprefix("RES05-") == "SUX"
                          and v.mean_w is not None and v.coverage_valid >= .5]
            fallback = False
            if not historical:
                historical = [v for v in layers if v.variable.removeprefix("RES05-") in {"SXX", "SXX30AS"}
                              and v.mean_w is not None and v.coverage_valid >= .5]
                fallback = bool(historical)
            if not historical:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="GAEZ_NO_COVERAGE"))
                continue
            layer = sorted(historical, key=lambda v: (-v.native_res_deg, v.layer_code))[0]
            central = layer.mean_w / 10000
            evidence = [central, *[c.value / 10000 for c in layer.per_cell
                         if c.value is not None and c.area_fraction >= .1 and 0 <= c.value <= 10000]]
            for fine in layers:
                if (fine.variable.removeprefix("RES05-") == "SXX30AS" and
                    fine.coverage_valid >= .5 and fine.mean_w is not None):
                    evidence.append(fine.mean_w / 10000)
            score = Interval(lo=0 if fallback or layer.coverage_valid < .8 else min(evidence),
                             mid=central, hi=max(evidence))
            if score.hi <= 0:
                below.append(NotEvaluatedCrop(crop_variant=crop.id, reason="GAEZ_ZERO_SUITABILITY"))
                continue
            scx = [v for v in layers if v.variable.removeprefix("RES05-") == "SCX"]
            shares = scx[0].class_shares if scx else None
            evaluations.append((crop, layer, score, shares, fallback))
    ordered = sorted(evaluations, key=lambda e: (-e[2].lo, -e[2].mid, e[0].id))
    results = []
    tier, leader_lo = 0, None
    grid = ["lower", "central", "upper"]
    scenario_ranks = [{entry[0].id: i for i, entry in enumerate(sorted(evaluations,
        key=lambda e: (-getattr(e[2], key), -e[2].mid, e[0].id)), 1)} for key in ("lo", "mid", "hi")]
    for rank, (crop, layer, score, shares, fallback) in enumerate(ordered[:req.options.max_results], 1):
        if leader_lo is None or score.hi < leader_lo:
            tier += 1
            leader_lo = score.lo
        ref = event("BASELINE_GAEZ", crop.id, score=score.model_dump(), wsim=wsim,
                    layer=layer.layer_code, source_sha256=layer.source_sha256,
                    fallback=fallback, coverage_valid=layer.coverage_valid,
                    disabled=["rotation", "sowing_windows", "weather_score", "economic_rank"])
        positions = [ranks[crop.id] for ranks in scenario_ranks]
        # Three endpoint orders describe only spatial spread, never full robustness.
        stability = StabilityResult(p_top5=sum(p <= 5 for p in positions) / 3,
            rank_range=(min(positions), max(positions)), rank_median=median(positions),
            label="unavailable", main_driver="spatial_GAEZ_endpoints_only", scenarios_evaluated=3)
        economic_request = req.model_copy(deep=True)
        if economic_request.economics is None:
            economic_request.economics = EconomicsInput(costs_per_ha=snap.costs_per_ha)
        reference_crop = crop.model_copy(update={"commodity_spec": crop.commodity_spec or "unspecified"})
        economics = compute_economics(reference_crop, economic_request, snap, rules)
        economics.applied_to_rank = False
        provided_cost = economic_request.economics.costs_per_ha.get(crop.id)
        if provided_cost and economics.costs_uah_ha is None:
            economics.costs_uah_ha = provided_cost.value
            economics.issues.append("COSTS_PER_T_NOT_TOTALLED_WITHOUT_YIELD")
        issues = ["AGRONOMY_NOT_VALIDATED", "GAEZ_CELL_NOT_FIELD_SOIL", "ROBUSTNESS_SPATIAL_ONLY"]
        if fallback:
            issues.append("GAEZ_FALLBACK_ALL_LAND")
        if layer.coverage_valid < .8:
            issues.append("GAEZ_PARTIAL_COVERAGE")
        results.append(RankedCrop(rank=rank, crop_variant=crop.id, name_uk=crop.name_uk,
            tier=tier, status="conditional", agro_score=score,
            gaez=GaezResult(layer=layer.layer_code, crop_code=crop.gaez_code, match=crop.gaez_match,
                scenario={"period": layer.period, "clim": layer.clim, "ssp": layer.ssp, "wsim": layer.wsim},
                suitability_index=Interval(**{key: getattr(score, key) * 100 for key in ("lo", "mid", "hi")}),
                class_shares=shares, coverage_valid=layer.coverage_valid,
                resolution_deg=layer.native_res_deg, field_to_cell_ratio=layer.field_to_cell_ratio),
            rotation=RotationResult(multiplier=Interval(lo=1, mid=1, hi=1)),
            weather=WeatherResult(multiplier=Interval(lo=1, mid=1, hi=1), signal="disabled"),
            satellite=snap.satellite, economics=economics, stability=stability,
            data_quality=DataQualitySummary(level="medium", issues=issues), assumptions=assumptions,
            reasons=[Explanation(text=f"Придатність GAEZ: {score.mid * 100:.1f} зі 100 за середнім кліматом 2001–2020.", trace_event_id=ref)],
            limitations=[Explanation(text="Умовний потенціал: посівне вікно та сівозміну не перевірено.", trace_event_id=ref)]))
    mode = "LIMITED" if results else "ABSTAIN"
    inputs = []
    if results and any("input_level=high (assumed)" == a for a in snap.assumptions):
        inputs.append(InputRequest(field="management.input_level", why="Рівень агротехнологій визначає шар GAEZ; зараз припущено high."))
    event("BASELINE_POLICY", mode=mode, profile="gaez_baseline", scope="UA",
          conditional_limit="up_to_5_potential_candidates", expert_review=False)
    used = {entry[1].layer_code: entry[1] for entry in evaluations}
    provenance = Provenance(algorithm_version=ALGORITHM_VERSION, rules_version=rules.version,
        snapshot_id=snap.snapshot_id,
        gaez_layers=[{"code": code, "sha256": layer.source_sha256, "source_url": layer.source_url}
                     for code, layer in sorted(used.items())], robustness_grid_hash=digest(grid),
        source_errors=snap.source_errors, weather=snap.weather.provenance, demo=snap.demo,
        price_ids=sorted({r.economics.price.price_id for r in results if r.economics and r.economics.price and r.economics.price.price_id}))
    identifier = "rec_" + digest({"snapshot": snap.model_dump(mode="json"), "request": req.model_dump(mode="json"),
                                  "rules": rules.model_dump(mode="json"), "algorithm": ALGORITHM_VERSION})[:32]
    return RecommendationResult(recommendation_id=identifier, status="ok" if results else "abstain",
        mode=mode, as_of_date=req.as_of_date, computed_at=snap.prepared_at, results=results,
        excluded=excluded, below_threshold=below, not_evaluated=not_evaluated,
        requests_for_input=inputs, data_quality=DataQualityReport(overall="medium" if results else "missing",
            by_source={"gaez": "medium" if results else "missing", "history": snap.history.quality,
                       "satellite": snap.satellite.quality, "weather": snap.weather.quality}),
        provenance=provenance, trace=trace)
