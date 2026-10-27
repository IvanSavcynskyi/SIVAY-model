"""Personalized risk ordering over real GAEZ, without invented yield multipliers."""
from datetime import date
from statistics import median

from app.canonical import digest
from app.domain import (Explanation, InputRequest, PersonalizationAssessment,
                        PersonalizationResult, RecommendationResult, StabilityResult, TraceEvent)
from app.ranking.baseline import recommend_baseline

ALGORITHM_VERSION = "personalized-1.0.0"
ALIASES = {"pea_dry": "pea", "maize_grain": "maize", "corn": "maize"}


def parent(identifier):
    value = identifier.lower().strip()
    return ALIASES.get(value, value.removesuffix("_winter").removesuffix("_spring"))


def _assessment(identifier, source, outcome, priority, text, **facts):
    return PersonalizationAssessment(rule_id=identifier, source=source, outcome=outcome,
        priority=priority if outcome in {"risk", "unknown"} else 0,
        explanation_uk=text, facts=facts)


def _rotation(crop, req, snap, policy):
    rows = [r for r in snap.history.records if r.status != "planned"
            and r.season_year < req.target_season.harvest_year]
    assessments = []
    if policy.avoid_same_crop_predecessor:
        recent = [r for r in rows if r.role != "cover_crop"
                  and r.season_year == req.target_season.harvest_year - 1]
        if not recent:
            outcome, text = "unknown", "Немає історії попереднього сезону для перевірки повторного посіву."
        else:
            repeated = [r for r in recent if parent(r.crop) == parent(crop.id)]
            outcome = "risk" if repeated else "pass"
            text = ("Ця культура була на полі в попередньому сезоні; знижено її пріоритет."
                    if repeated else "Повторний посів тієї самої культури за наданою історією не виявлено.")
        assessments.append(_assessment("rotation.predecessor", policy.source, outcome, 2, text,
            records=[r.model_dump(mode="json") for r in recent]))
    minimum = policy.min_return_years.get(crop.id, policy.min_return_years.get(parent(crop.id)))
    avoided = policy.avoid_predecessors.get(crop.id, policy.avoid_predecessors.get(parent(crop.id)))
    if avoided:
        recent = [r for r in rows if r.role != "cover_crop"
                  and r.season_year == req.target_season.harvest_year - 1]
        outcome = ("unknown" if not recent else "risk" if any(
            parent(r.crop) in {parent(c) for c in avoided} for r in recent) else "pass")
        assessments.append(_assessment("rotation.farm_predecessors", policy.source, outcome, 2,
            "Перевірка небажаних попередників за правилами господарства: " + outcome + ".",
            avoided_predecessors=avoided, observed_predecessors=[r.crop for r in recent]))
    if minimum:
        recent_years = set(range(req.target_season.harvest_year - minimum + 1,
                                 req.target_season.harvest_year))
        repeated = [r for r in rows if parent(r.crop) == parent(crop.id) and r.season_year in recent_years]
        known_years = {r.season_year for r in rows if r.role == "main"}
        gaps = recent_years - known_years
        if repeated:
            outcome, text = "risk", f"Не витримано заданий інтервал повернення культури: {minimum} років."
        elif gaps:
            outcome, text = "unknown", "Історія неповна для перевірки інтервалу повернення культури."
        else:
            outcome, text = "pass", "За наданою історією інтервал повернення культури витримано."
        assessments.append(_assessment("rotation.return", policy.source, outcome, 3, text,
            min_return_years=minimum, missing_years=sorted(gaps),
            observed_years=sorted({r.season_year for r in repeated})))
    return assessments


def _rule(rule, crop, req, snap):
    if not any(selector == "*" or selector == crop.id or selector == parent(crop.id)
               for selector in rule.crops):
        return None
    facts = {"metric": rule.metric, "operator": rule.operator, "threshold": rule.threshold,
             "horizon_days": rule.horizon_days}
    plans = snap.field_signals.weather.get("sowing_dates", {})
    sowing = plans.get(crop.id, plans.get(parent(crop.id)))
    if sowing is None:
        return _assessment(rule.id, rule.source, "unknown", rule.priority,
                           "Потрібна запланована дата посіву для застосування поточних умов.", **facts)
    sowing = date.fromisoformat(sowing)
    facts["sowing_date"] = sowing.isoformat()
    expected_year = req.target_season.harvest_year - (crop.season_type == "winter")
    if sowing.year != expected_year:
        return _assessment(rule.id, rule.source, "unknown", rule.priority,
            "Дата посіву не відповідає цільовому сезону цієї культури; уточніть план посіву.", **facts)
    horizon = min(7, rule.horizon_days) if rule.metric.startswith("forecast.") else rule.horizon_days
    if not 0 <= (sowing - req.as_of_date).days <= horizon:
        return _assessment(rule.id, rule.source, "not_applicable", 0,
            "Умови на поточну дату не поширюються на цей строк посіву; потрібне оновлення ближче до посіву.", **facts)
    value = snap.field_signals.metrics.get(rule.metric)
    facts.update(value=value, sources=snap.field_signals.metric_sources.get(rule.metric, []))
    if value is None:
        return _assessment(rule.id, rule.source, "unknown", rule.priority,
            "Для правила немає свіжих даних достатньої якості; пропуск не вважається сприятливими умовами.", **facts)
    risk = value < rule.threshold if rule.operator == "lt" else value > rule.threshold
    return _assessment(rule.id, rule.source, "risk" if risk else "pass", rule.priority,
        rule.explanation_uk if risk else f"Умова ризику «{rule.id}» не спрацювала.", **facts)


def recommend_personalized(req, snap, rules):
    result = recommend_baseline(req, snap, rules).model_copy(deep=True)
    policy = snap.field_signals.policy
    if policy is None or not result.results:
        return result
    candidates, trace = [], list(result.trace)
    for variant in rules.catalog:
        # Evaluate every eligible candidate before truncating to five; otherwise
        # a previously sixth crop could never replace a risky top-five crop.
        single_rules = rules.model_copy(update={"catalog": [variant]})
        single = recommend_baseline(req, snap, single_rules)
        if not single.results:
            continue
        candidate = single.results[0]
        assessments = _rotation(variant, req, snap, policy)
        assessments += [assessment for rule in policy.rules
                        if (assessment := _rule(rule, variant, req, snap)) is not None]
        priority = max((a.priority for a in assessments), default=0)
        candidate.personalization = PersonalizationResult(policy_version=policy.version,
            policy_status=policy.status, risk_priority=priority, assessments=assessments,
            metrics=snap.field_signals.metrics, sources=snap.field_signals.metric_sources,
            evidence={"indices": snap.field_signals.indices, "images": snap.field_signals.images,
                      "weather": snap.field_signals.weather, "issues": snap.field_signals.issues})
        identifier = "personalize:" + variant.id
        trace.append(TraceEvent(id=identifier, code="PERSONALIZATION", crop=variant.id,
            params=candidate.personalization.model_dump(mode="json"),
            source_refs=sorted({s for sources in snap.field_signals.metric_sources.values() for s in sources})))
        candidate.reasons = [Explanation(text="Персоналізація за даними поля.", trace_event_id=identifier)]
        candidate.limitations = []
        candidate.stability = StabilityResult(p_top5=0, rank_range=(1, max(1, len(rules.catalog))),
            rank_median=None, label="unavailable", main_driver="personalization_not_calibrated")
        candidate.data_quality.issues += ["PERSONALIZATION_NOT_FIELD_VALIDATED", *snap.field_signals.issues]
        candidates.append(candidate)
    def order(candidate):
        assessments = candidate.personalization.assessments
        return (candidate.personalization.risk_priority,
                sum(a.outcome in {"risk", "unknown"} for a in assessments),
                -candidate.agro_score.lo, -candidate.agro_score.mid, candidate.crop_variant)
    candidates.sort(key=order)
    endpoint_ranks = []
    for endpoint in ("lo", "mid", "hi"):
        ordered = sorted(candidates, key=lambda c: (*order(c)[:2], -getattr(c.agro_score, endpoint),
                                                   -c.agro_score.mid, c.crop_variant))
        endpoint_ranks.append({c.crop_variant: rank for rank, c in enumerate(ordered, 1)})
    for rank, candidate in enumerate(candidates[:req.options.max_results], 1):
        candidate.rank, candidate.tier = rank, rank
        positions = [ranks[candidate.crop_variant] for ranks in endpoint_ranks]
        candidate.stability = StabilityResult(p_top5=sum(p <= 5 for p in positions) / 3,
            rank_range=(min(positions), max(positions)), rank_median=median(positions),
            label="unavailable", main_driver="fixed_policy_GAEZ_endpoints_only", scenarios_evaluated=3)
    result.results = candidates[:req.options.max_results]
    result.trace = trace
    result.mode = "LIMITED" if result.results else "ABSTAIN"
    result.data_quality.by_source.update(
        field_indices="medium" if snap.field_signals.indices else "missing",
        imagery="medium" if snap.field_signals.images else "missing",
        local_weather="medium" if any(k.startswith("weather.") for k in snap.field_signals.metrics) else "missing")
    needed = {(item.field, item.why) for item in result.requests_for_input}
    if not snap.history.records:
        needed.add(("crop_history", "Додайте історію культур за попередні роки для перевірки сівозміни."))
    if not policy.rules:
        needed.add(("personalization.policy.rules",
            "Додайте правила господарства для впливу NDVI/NDMI та локальної погоди; універсальних порогів немає."))
    for candidate in result.results:
        for assessment in candidate.personalization.assessments:
            if assessment.outcome == "unknown":
                field = "crop_history" if assessment.rule_id.startswith("rotation.") else (
                    "personalization.sowing_dates" if "sowing_date" not in assessment.facts else assessment.facts["metric"])
                needed.add((field, assessment.explanation_uk))
    result.requests_for_input = [InputRequest(field=field, why=why) for field, why in sorted(needed)]
    result.provenance.algorithm_version = ALGORITHM_VERSION
    result.provenance.rules_version += "+" + policy.version + "+" + digest(policy.model_dump(mode="json"))[:12]
    result.recommendation_id = "rec_" + digest({"algorithm": ALGORITHM_VERSION,
        "request": req.model_dump(mode="json"), "snapshot": snap.model_dump(mode="json"),
        "rules": rules.model_dump(mode="json")})[:32]
    return RecommendationResult.model_validate(result.model_dump())
