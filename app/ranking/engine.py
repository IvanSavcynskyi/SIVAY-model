"""Conservative interval ranking. No network, wall clock, randomness or writes.

The lower bound is the primary agronomic key. See the integration ADR for the
conflict between exact economic slots and the absolute missingness invariant.
"""
from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from itertools import product
import json
from math import prod
from statistics import median

from app.domain.models import (
    CropVariant, DataQualityReport, DataQualitySummary, EconomicsResult, ExcludedCrop,
    Explanation, FieldFeatureSnapshot, GaezFieldValue, GaezResult, InputRequest, Interval,
    NotEvaluatedCrop, Provenance, RankedCrop, RecommendationRequest, RecommendationResult,
    RotationResult, RuleFired, RuleSet, StabilityResult, TraceEvent, WeatherResult,
    as_of_cutoff,
)

ALGORITHM_VERSION = "interval-conservative-1.0.0"
ONE = Interval(lo=1, mid=1, hi=1)


def _hash(value) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _active(status: str, demo: bool) -> bool:
    return status == "approved" or (status == "demo" and demo)


def _parent(crop: CropVariant) -> str:
    if crop.parent_crop:
        return crop.parent_crop
    for suffix in ("_winter", "_spring", "_grain", "_silage", "_dry"):
        if crop.id.endswith(suffix):
            return crop.id[:-len(suffix)]
    return crop.id


def _matches(record, crop: CropVariant) -> bool:
    return record.variant == crop.id or record.crop in {crop.id, _parent(crop)}


def _wsim(req: RecommendationRequest) -> str:
    return ("H" if req.management.input_level == "high" else "L") + ("I" if req.management.irrigation == "irrigated" else "R") + "LM"


def _variable(layer: GaezFieldValue) -> str:
    return layer.variable.upper().split("-")[-1]


def gaez_interval(layers: list[GaezFieldValue], wsim: str) -> tuple[Interval, GaezFieldValue | None, list[str]]:
    """Area-weighted SUX plus explicit cell/fine/future spread, with honest gaps."""
    relevant = [v for v in layers if v.wsim == wsim]
    historical = [v for v in relevant if v.period == "HP0120" and v.ssp == "HIST"]
    sux = [v for v in historical if _variable(v) == "SUX" and v.mean_w is not None and v.coverage_valid >= .5]
    fallback = False
    if not sux:
        sux = [v for v in historical if _variable(v) in {"SXX", "SXX30AS"} and v.mean_w is not None and v.coverage_valid >= .5]
        fallback = bool(sux)
    if not sux:
        return Interval(lo=0, mid=0, hi=0), None, ["GAEZ_NO_COVERAGE"]
    base = sorted(sux, key=lambda v: (-v.native_res_deg, v.layer_code))[0]
    mid = max(0, min(1, base.mean_w / 10000))
    values = [mid]
    values += [v.value / 10000 for v in base.per_cell if v.value is not None and 0 <= v.value <= 10000 and v.area_fraction >= .1]
    for layer in relevant:
        fine = layer.period == "HP0120" and _variable(layer) in {"SXX", "SXX30AS"} and layer.native_res_deg < base.native_res_deg
        future = layer.period == "FP2140" and layer.clim == "ENSEMBLE" and layer.ssp in {"SSP126", "SSP370", "SSP585"} and _variable(layer) == "SUX"
        if (fine or future) and layer.mean_w is not None and layer.coverage_valid >= .5:
            values.append(max(0, min(1, layer.mean_w / 10000)))
    issues = []
    lo, hi = min(values), max(values)
    if fallback:
        lo = min(lo, .8 * mid)
        issues.append("gaez_fallback_all_land")
    if base.coverage_valid < .8:
        # Snapshot has no buffer-neighbour contract. Zero is the defensible lower
        # bound; never fabricate the unavailable neighbouring-cell minimum.
        lo = 0
        issues.append("gaez_partial_coverage_no_buffer")
    return Interval(lo=lo, mid=mid, hi=hi), base, issues


def _window_dates(window, crop: CropVariant, year: int):
    sow_year = year - 1 if crop.season_type == "winter" else year
    start = date.fromisoformat(f"{sow_year}-{window.earliest}")
    end = date.fromisoformat(f"{sow_year}-{window.latest}")
    if end < start:
        end = end.replace(year=end.year + 1)
    return start, end


def _harvest_bounds(record):
    if record.harvest_date_planned:
        return record.harvest_date_planned.from_date, record.harvest_date_planned.to
    value = record.harvest_date
    if value is None:
        return None
    if record.harvest_date_precision == "month":
        return value.replace(day=1), value.replace(day=monthrange(value.year, value.month)[1])
    if record.harvest_date_precision == "season":
        return date(value.year, 1, 1), date(value.year, 12, 31)
    return value, value


@dataclass
class Evaluation:
    crop: CropVariant
    gaez: Interval
    layer: GaezFieldValue
    rotation: RotationResult
    weather: WeatherResult
    score: Interval
    economics: EconomicsResult | None
    issues: list[str]
    event_id: str
    conditional: bool = False
    tier: int = 0


def _rotation(crop, req, snap, rules, demo):
    """Worst admissible completion supplies lo; unreliable facts never ban."""
    history = snap.history
    records = history.records
    catalog = {c.id: c for c in rules.catalog}
    predecessor = records[-1] if records else None
    params = rules.parameters
    fired, factors = [], []
    uncertain = not records or bool(history.gaps) or any(r.reliability == "low" for r in records)
    hard = False
    conditional = False
    for rule in sorted(rules.rotation, key=lambda r: r.id):
        target = rule.applies_to
        if not _active(rule.status, demo) or snap.region not in rule.regions:
            continue
        if target.candidate and target.candidate not in {crop.id, _parent(crop)}:
            continue
        if target.candidate_family and target.candidate_family != crop.family:
            continue
        if target.season_type and target.season_type != crop.season_type:
            continue
        condition = rule.condition
        def family_match(row):
            matching = [c for c in catalog.values() if _matches(row, c)]
            return any(c.family == crop.family for c in matching)
        dependent = []
        if condition.type in {"same_crop", "same_family", "predecessor_is"}:
            if predecessor:
                matches = (_matches(predecessor, crop) if condition.type == "same_crop" else
                           family_match(predecessor) if condition.type == "same_family" else
                           predecessor.crop == condition.predecessor or predecessor.variant == condition.predecessor)
                if matches:
                    dependent = [predecessor]
        elif condition.type == "return_interval":
            years = condition.min_years_between or condition.years
            if years:
                dependent = [row for row in records if 0 < req.target_season.harvest_year - row.season_year < years and
                             (family_match(row) if condition.scope == "same_family" else _matches(row, crop))]
                expected = set(range(req.target_season.harvest_year-years+1, req.target_season.harvest_year))
                if not expected <= {row.season_year for row in records if row.reliability != "low"}:
                    uncertain = True
        elif condition.type == "harvest_sowing_gap" and predecessor:
            window = next((w for w in rules.sowing_windows if w.crop_variant == crop.id and w.region == snap.region and _active(w.status, demo) and w.earliest), None)
            bounds = _harvest_bounds(predecessor)
            if window and bounds and condition.min_days is not None:
                start, _ = _window_dates(window, crop, req.target_season.harvest_year)
                if (start-bounds[1]).days < condition.min_days:
                    dependent = [predecessor]
        if not dependent:
            continue
        reliable = all(row.reliability in {"high", "medium"} for row in dependent)
        effect = rule.effect
        if effect == "incompatible":
            hard |= reliable
            conditional |= not reliable
            if not reliable:
                effect = "discouraged_strong"
        fired.append(RuleFired(id=rule.id, version=rule.version, effect=effect))
        if effect != "incompatible":
            multiplier = params.effect_multipliers.get(effect)
            if multiplier is not None:
                factors.append(multiplier)
        uncertain |= not reliable
    low_limit = params.effect_multipliers["discouraged_strong"]
    high_limit = params.effect_multipliers["favorable"]
    central = max(low_limit, min(high_limit, prod(factors)))
    lo, hi = central, central
    if uncertain:
        lo = min(lo, params.rotation_missing.lo)
        hi = max(hi, params.rotation_missing.hi)
    return RotationResult(multiplier=Interval(lo=lo, mid=central, hi=hi), rules_fired=fired), hard, conditional, uncertain


def _economics(crop, req, snap, rules):
    try:
        from app.features.economics import compute_economics
    except ImportError:
        return None
    return compute_economics(crop, req, snap, rules)


def build_tiers(evaluations: list[Evaluation], *, rule: str = "overlap") -> list[list[Evaluation]]:
    """Conservative leaders and noncompensatory boundaries, stable on input order."""
    remaining = sorted(evaluations, key=lambda e: (-e.score.lo, -e.score.mid, e.crop.id))
    tiers = []
    while remaining:
        leader = remaining[0]
        tier = []
        for candidate in remaining:
            overlaps = candidate.score.hi >= leader.score.lo if rule == "overlap" else abs(candidate.score.mid-leader.score.mid) <= .05
            if not overlaps:
                break
            tier.append(candidate)
        remaining = remaining[len(tier):]
        tiers.append(tier)
    return tiers


def _ordered(evaluations, *, tier_rule="overlap", price="base", cost_factor=1.0):
    result = []
    for number, tier in enumerate(build_tiers(evaluations, rule=tier_rule), 1):
        def key(e):
            eco = e.economics
            margin = float("-inf")
            if eco and eco.complete and eco.margin_uah_ha:
                margin = getattr(eco.margin_uah_ha, price)
                if eco.costs_uah_ha is not None:
                    margin -= eco.costs_uah_ha * (cost_factor-1)
            # Economics breaks exact conservative agronomic ties only. Missing
            # economics has the worst key, hence deleting it cannot improve rank.
            return (-e.score.lo, -margin, -e.score.mid, e.crop.id)
        for item in sorted(tier, key=key):
            result.append((item, number))
    return result


def _select(ordered, limit):
    unconditionals = [(e, t) for e, t in ordered if not e.conditional]
    last_tier = unconditionals[min(len(unconditionals), limit)-1][1] if unconditionals else None
    result = []
    conditional_count = 0
    for evaluation, tier in ordered:
        if evaluation.conditional:
            if conditional_count >= 2 or last_tier is None or tier > last_tier:
                continue
            conditional_count += 1
        result.append((evaluation, tier))
        if len(result) == limit:
            break
    return result


def recommend(req: RecommendationRequest, snap: FieldFeatureSnapshot, rules: RuleSet) -> RecommendationResult:
    """Evaluate one immutable snapshot and versioned rules at the request cutoff."""
    demo = bool(rules.demo and snap.demo)
    cutoff = as_of_cutoff(req.as_of_date)
    trace = []
    def event(code, crop=None, **params):
        identifier = f"e{len(trace)+1:05d}"
        trace.append(TraceEvent(id=identifier, code=code, crop=crop, params=params))
        return identifier
    excluded, not_evaluated, below, inputs, evaluations = [], [], [], [], []
    params = rules.parameters
    valid_params = (_active(params.status, demo) and params.floor_agro is not None and
                    params.rotation_missing is not None and params.weather_missing is not None and
                    params.nowcast_horizon_days is not None and
                    all(params.effect_multipliers.get(k) is not None for k in ("discouraged_strong", "neutral", "favorable")))
    seasons = set(req.target_season.include)
    if "spring" in seasons:
        seasons.add("summer")
    candidates = [c for c in rules.catalog if c.season_type in seasons]
    supported = snap.region is not None and any(_active(c.status, demo) and snap.region in c.regions for c in candidates)
    invalid_context = snap.field_id != req.field.field_id or snap.as_of_date != req.as_of_date
    if rules.demo != snap.demo:
        invalid_context = True
    if invalid_context or not supported or not valid_params:
        code = "SNAPSHOT_CONTEXT_MISMATCH" if invalid_context else "REGION_NOT_SUPPORTED" if not supported else "UNAPPROVED_PARAMETERS"
        event(code)
        for crop in candidates:
            not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason=code))
    else:
        for crop in sorted(candidates, key=lambda c: c.id):
            if not _active(crop.status, demo) or snap.region not in crop.regions:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="CATALOG_NOT_APPROVED"))
                continue
            def exclude(gate, text):
                identifier = event(gate, crop.id, reason=text)
                excluded.append(ExcludedCrop(crop_variant=crop.id, gate=gate, text=text, trace_event_id=identifier))
            if {crop.id, _parent(crop)} & set(req.constraints.excluded_crops):
                exclude("G1", "Культуру виключено обмеженням користувача.")
                continue
            event("G1", crop.id, outcome="PASS")
            if not crop.gaez_code:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="UNSUPPORTED_GAEZ_CROP"))
                continue
            window = next((w for w in rules.sowing_windows if w.crop_variant == crop.id and w.region == snap.region and _active(w.status, demo) and w.earliest and w.latest and w.min_lead_days is not None and w.prep_days is not None), None)
            if window is None:
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="NO_APPROVED_SOWING_WINDOW"))
                continue
            start, end = _window_dates(window, crop, req.target_season.harvest_year)
            if req.as_of_date + timedelta(days=window.min_lead_days) > end:
                exclude("G2", "Посівне вікно вже минуло.")
                continue
            conditional = False
            predecessor = snap.history.current_crop
            if predecessor:
                bounds = _harvest_bounds(predecessor)
                reliable = predecessor.reliability in {"high", "medium"}
                if bounds and reliable and bounds[0] + timedelta(days=window.prep_days) > end:
                    exclude("G2", "Збирання попередника та підготовка не вкладаються у посівне вікно.")
                    continue
                if bounds is None or not reliable or bounds[1] + timedelta(days=window.prep_days) > end:
                    conditional = True
                    inputs.append(InputRequest(field="crop_history.harvest_date_planned", why=f"Підтвердьте збирання попередника для {crop.name_uk}."))
            elif crop.season_type == "winter" and snap.history.occupied is None:
                conditional = True
                inputs.append(InputRequest(field="crop_history.harvest_date_planned", why=f"Підтвердьте, що поле буде вільним до посіву {crop.name_uk}."))
            event("G2", crop.id, outcome="CONDITIONAL" if conditional else "PASS", window_start=str(start), window_end=str(end))
            rotation, incompatible, uncertain_rotation, missing_rotation = _rotation(crop, req, snap, rules, demo)
            if incompatible:
                exclude("G3", "Несумісність підтверджена затвердженим правилом і надійною історією.")
                continue
            conditional |= uncertain_rotation
            if uncertain_rotation:
                inputs.append(InputRequest(field="crop_history", why=f"Підтвердьте історію для перевірки несумісності {crop.name_uk}."))
            event("G3", crop.id, outcome="CONDITIONAL" if uncertain_rotation else "PASS", rules=[r.model_dump() for r in rotation.rules_fired])
            layers = [v for v in snap.gaez.get(crop.gaez_code, []) if v.available_at <= cutoff]
            matching = [v for v in layers if v.wsim == _wsim(req)]
            scx = [v for v in matching if _variable(v) in {"SCX", "SCX30AS"} and v.coverage_valid >= .5]
            successful = any(_matches(r, crop) and r.reliability in {"medium", "high"} and r.status == "harvested" and r.yield_t_ha is not None for r in snap.history.records)
            # Absence of the fine or future layer cannot serve as corroboration.
            corroborated = all(any(v.period == period and (v.native_res_deg < .02) == fine and (v.class_shares or {}).get(8, 0) >= .9 for v in scx) for period, fine in product(("HP0120", "FP2140"), (False, True)))
            if corroborated and not successful:
                exclude("G4", "Непридатність GAEZ підтверджена двома роздільностями та періодами.")
                continue
            event("G4", crop.id, outcome="PASS", corroborated=corroborated, farm_success=successful)
            gaez, layer, issues = gaez_interval(layers, _wsim(req))
            if layer is None:
                identifier = event("G5", crop.id, reason="GAEZ_NO_COVERAGE")
                not_evaluated.append(NotEvaluatedCrop(crop_variant=crop.id, reason="GAEZ_NO_COVERAGE", trace_event_id=identifier))
                continue
            event("G5", crop.id, outcome="PASS", coverage=layer.coverage_valid)
            if any((v.class_shares or {}).get(8, 0) >= .9 for v in scx) and not corroborated:
                issues.append("coarse_gaez_conflict")
            weather_multiplier = ONE
            signal = "not_applicable"
            if crop.establishment_moisture_sensitive and start <= req.as_of_date + timedelta(days=params.nowcast_horizon_days):
                wx = snap.weather
                available = wx.available_at is not None and wx.available_at <= cutoff and wx.quality != "missing"
                signal = wx.signal if available else "unknown"
                weather_multiplier = params.weather_multipliers.get(signal) if signal != "unknown" else None
                if weather_multiplier is None:
                    weather_multiplier = params.weather_missing
                    issues.append("weather_missing")
            if missing_rotation:
                issues.append("history_incomplete")
            weather = WeatherResult(multiplier=weather_multiplier, signal=signal, risks=snap.weather.risks if signal not in {"unknown", "not_applicable"} else [])
            score = gaez.product(rotation.multiplier, weather_multiplier)
            identifier = event("AGRO_SCORE", crop.id, a=gaez.model_dump(), rotation=rotation.model_dump(), weather=weather.model_dump(), agro=score.model_dump(), issues=issues)
            if score.hi < params.floor_agro:
                below.append(NotEvaluatedCrop(crop_variant=crop.id, reason="BELOW_AGRO_THRESHOLD", trace_event_id=identifier))
                continue
            evaluations.append(Evaluation(crop, gaez, layer, rotation, weather, score, _economics(crop, req, snap, rules), issues, identifier, conditional))
    ordered = _ordered(evaluations)
    selected = _select(ordered, req.options.max_results)
    # Deterministic Cartesian perturbation grid, recomputing scores and complete
    # order rather than synthesising probabilities from interval widths.
    axes = {"rotation": [0.5, 1.0, 1.5], "weather": [True, False], "period": ["HP0120"], "tier": ["overlap", "mid"]}
    assumed_management = any("input_level=" in assumption and "assumed" in assumption for assumption in snap.assumptions)
    alternate_wsim = ("L" if req.management.input_level == "high" else "H") + _wsim(req)[1:]
    management_missing = assumed_management and any(gaez_interval([v for v in snap.gaez.get(e.crop.gaez_code, []) if v.available_at <= cutoff], alternate_wsim)[1] is None for e in evaluations)
    if assumed_management and evaluations:
        if management_missing:
            inputs.append(InputRequest(field="management.input_level", why="Уточніть рівень агротехнологій: альтернативні шари GAEZ відсутні."))
        else:
            axes["management"] = [_wsim(req), alternate_wsim]
    if any(v.period == "FP2140" and v.ssp == "SSP370" and v.clim == "ENSEMBLE" and v.available_at <= cutoff for ls in snap.gaez.values() for v in ls):
        axes["period"].append("FP2140")
    if any(e.economics and e.economics.complete for e in evaluations):
        axes["price"] = ["low", "base", "high"]
        fraction = params.robustness_cost_fraction or 0
        axes["cost"] = sorted(set([1-fraction, 1, 1+fraction]))
    if prod(len(values) for values in axes.values()) > 300 and "cost" in axes:
        axes["cost"] = [axes["cost"][0], axes["cost"][-1]]
    grid = [dict(zip(axes, values)) for values in product(*axes.values())]
    ranks = {e.crop.id: [] for e in evaluations}
    memberships = {e.crop.id: [] for e in evaluations}
    for scenario in grid:
        altered = []
        for e in evaluations:
            rot = e.rotation.multiplier
            factor = scenario["rotation"]
            perturbed = Interval(**{key: max(0, 1 + factor*(getattr(rot, key)-1)) for key in ("lo", "mid", "hi")})
            a = e.gaez
            scenario_wsim = scenario.get("management", _wsim(req))
            if scenario_wsim != _wsim(req):
                a, _, _ = gaez_interval([v for v in snap.gaez.get(e.crop.gaez_code, []) if v.available_at <= cutoff], scenario_wsim)
            if scenario["period"] == "FP2140":
                future = [v for v in snap.gaez.get(e.crop.gaez_code, []) if v.period == "FP2140" and v.ssp == "SSP370" and v.clim == "ENSEMBLE" and v.wsim == scenario_wsim and _variable(v) == "SUX" and v.coverage_valid >= .5 and v.mean_w is not None and v.available_at <= cutoff]
                if future:
                    mid = max(0, min(1, sorted(future, key=lambda v: v.layer_code)[0].mean_w/10000))
                    a = Interval(lo=min(a.lo, mid), mid=mid, hi=max(a.hi, mid))
            score = a.product(perturbed, e.weather.multiplier if scenario["weather"] else ONE)
            if score.hi >= (params.floor_agro or 0):
                from dataclasses import replace
                altered.append(replace(e, score=score))
        scenario_order = _ordered(altered, tier_rule=scenario["tier"], price=scenario.get("price", "base"), cost_factor=scenario.get("cost", 1))
        scenario_selected = {e.crop.id for e, _ in _select(scenario_order, 5)}
        positions = {e.crop.id: position for position, (e, _) in enumerate(scenario_order, 1)}
        for e in evaluations:
            ranks[e.crop.id].append(positions.get(e.crop.id, len(evaluations)+1))
            memberships[e.crop.id].append(e.crop.id in scenario_selected)
    results = []
    for rank, (e, tier) in enumerate(selected, 1):
        positions = ranks[e.crop.id]
        p_top = sum(memberships[e.crop.id])/len(grid)
        label = "stable" if p_top >= .8 and max(positions)-min(positions) <= 2 else "sensitive" if p_top >= .5 else "unstable"
        driver = None
        if management_missing:
            label, driver = "unavailable", "management_data_missing"
        elif label != "stable":
            effects = {}
            for axis, values in axes.items():
                means = [sum(pos for pos, g in zip(positions, grid) if g[axis] == value)/sum(g[axis] == value for g in grid) for value in values]
                effects[axis] = max(means)-min(means)
            driver = max(effects, key=effects.get)
        stability = StabilityResult(p_top5=p_top, rank_range=(min(positions), max(positions)), rank_median=median(positions), label=label, main_driver=driver, scenarios_evaluated=len(grid))
        eco = e.economics.model_copy(deep=True) if e.economics else None
        if eco:
            eco.applied_to_rank = bool(eco.complete and any(other.crop.id != e.crop.id and other.score.lo == e.score.lo and other.economics and other.economics.complete for other, other_tier in ordered if other_tier == tier))
        results.append(RankedCrop(rank=rank, crop_variant=e.crop.id, name_uk=e.crop.name_uk, tier=tier, status="conditional" if e.conditional else "recommended", agro_score=e.score,
            gaez=GaezResult(layer=e.layer.layer_code, crop_code=e.crop.gaez_code, match=e.crop.gaez_match, scenario={"period": e.layer.period, "clim": e.layer.clim, "ssp": e.layer.ssp, "wsim": e.layer.wsim}, suitability_index=Interval(**{key: getattr(e.gaez, key)*100 for key in ("lo", "mid", "hi")}), class_shares=next((v.class_shares for v in snap.gaez.get(e.crop.gaez_code, []) if _variable(v) == "SCX" and v.wsim == _wsim(req) and v.period == "HP0120" and v.available_at <= cutoff), e.layer.class_shares), coverage_valid=e.layer.coverage_valid, resolution_deg=e.layer.native_res_deg, field_to_cell_ratio=e.layer.field_to_cell_ratio),
            rotation=e.rotation, weather=e.weather, satellite=snap.satellite, economics=eco, stability=stability,
            data_quality=DataQualitySummary(level="low" if e.issues else "high", issues=e.issues),
            assumptions=snap.assumptions, reasons=[Explanation(text="Оцінка відтворюється з придатності GAEZ, сівозміни та умов посіву.", trace_event_id=e.event_id)],
            limitations=[Explanation(text=issue, trace_event_id=e.event_id) for issue in e.issues],
            near_tie_with=[other.crop.id for other, other_tier in ordered if other.crop.id != e.crop.id and other_tier == tier and abs(other.score.mid-e.score.mid) < (params.near_tie_epsilon or 0)]))
    if not snap.history.records and evaluations:
        inputs.append(InputRequest(field="crop_history", why="Додайте історію культур за 3–5 років."))
    management_changed = False
    if "management" in axes:
        for e in evaluations:
            paired = {}
            for member, scenario in zip(memberships[e.crop.id], grid):
                key = tuple((axis, value) for axis, value in scenario.items() if axis != "management")
                paired.setdefault(key, set()).add(member)
            if any(len(outcomes) > 1 for outcomes in paired.values()):
                management_changed = True
                break
        if management_changed:
            inputs.append(InputRequest(field="management.input_level", why="Уточніть рівень агротехнологій: склад топ-5 змінюється між сценаріями."))
    mode = "ABSTAIN"
    status = "abstain"
    if results:
        limited = any(e.issues or e.conditional for e in evaluations)
        full = any(r.economics and r.economics.complete and r.tier == 1 for r in results)
        mode = "LIMITED" if limited else "FULL" if full else "AGRO_ONLY"
        status = "ok"
        if management_missing or management_changed or (inputs and any(r.stability.p_top5 < .5 for r in results)):
            mode, status = "NEEDS_INPUT", "needs_input"
    elif evaluations and inputs:
        mode, status = "NEEDS_INPUT", "needs_input"
    event("FINAL_SELECTION", mode=mode, selected=[r.crop_variant for r in results], ordering="conservative_lower_bound")
    quality = DataQualityReport(overall="low" if mode in {"LIMITED", "NEEDS_INPUT", "ABSTAIN"} else "high", by_source={"history": snap.history.quality, "weather": snap.weather.quality, "satellite": snap.satellite.quality, "gaez": "high" if evaluations else "missing"})
    provenance = Provenance(algorithm_version=ALGORITHM_VERSION, rules_version=rules.version, snapshot_id=snap.snapshot_id, gaez_layers=[{"layer_code": v.layer_code, "sha256": v.source_sha256} for values in snap.gaez.values() for v in sorted(values, key=lambda v: v.layer_code) if v.available_at <= cutoff], weather=snap.weather.provenance, price_ids=sorted({r.economics.price.price_id for r in results if r.economics and r.economics.price and r.economics.price.price_id}), robustness_grid_hash=_hash(grid), source_errors=snap.source_errors, demo=demo)
    identifier = _hash({"request": req.model_dump(mode="json"), "snapshot": snap.model_dump(mode="json"), "rules": rules.model_dump(mode="json"), "algorithm": ALGORITHM_VERSION})
    return RecommendationResult(recommendation_id=identifier, status=status, mode=mode, as_of_date=req.as_of_date, computed_at=snap.prepared_at, results=results, excluded=excluded, below_threshold=below, not_evaluated=not_evaluated, requests_for_input=inputs, data_quality=quality, provenance=provenance, trace=trace)
