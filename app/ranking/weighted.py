"""Bounded, monotone field adjustments; weights are policy parameters, not yield losses."""
from app.domain import Interval, WeightedCalculation, WeightedContribution, WeightedGroup


def rule_group(rule):
    if rule.group:
        return rule.group
    if rule.metric.startswith("NDVI."):
        return "vegetation"
    if ".tmin_" in rule.metric or ".tmax_" in rule.metric:
        return "temperature"
    return "moisture"


def calculate_weighted(base, assessments, policy):
    rules = {rule.id: rule for rule in policy.rules}
    contributions = []
    for assessment in assessments:
        rule = rules.get(assessment.rule_id)
        if rule is None or assessment.rule_id.startswith("rotation."):
            continue
        if assessment.outcome == "unknown":
            # Conservative central estimate. Removing evidence cannot improve
            # either ranking endpoint; the upper bound retains neutral potential.
            severity = Interval(lo=0, mid=1, hi=1)
        elif assessment.outcome != "risk":
            severity = Interval(lo=0, mid=0, hi=0)
        else:
            value = assessment.facts["value"]
            magnitude = (1 if rule.full_risk_at is None else
                         min(1, max(0, (value - rule.threshold) / (rule.full_risk_at - rule.threshold))))
            severity = Interval(lo=magnitude, mid=magnitude, hi=magnitude)
        penalty = Interval(**{key: getattr(severity, key) * rule.weight for key in ("lo", "mid", "hi")})
        contributions.append(WeightedContribution(rule_id=rule.id, group=rule_group(rule), weight=rule.weight,
            outcome=assessment.outcome, severity=severity, penalty=penalty, source=rule.source))
    groups = []
    for group in sorted({entry.group for entry in contributions}):
        entries = [entry for entry in contributions if entry.group == group]
        # Correlated measurements share a bounded group; adding duplicate rules
        # never compounds the same stress several times.
        penalty = Interval(**{key: max(getattr(e.penalty, key) for e in entries) for key in ("lo", "mid", "hi")})
        factor = Interval(lo=1-penalty.hi, mid=1-penalty.mid, hi=1-penalty.lo)
        groups.append(WeightedGroup(group=group, rule_ids=sorted(e.rule_id for e in entries),
                                    penalty=penalty, multiplier=factor))
    multiplier = Interval(lo=1, mid=1, hi=1).product(*(g.multiplier for g in groups))
    return WeightedCalculation(base_score=base, multiplier=multiplier, final_score=base.product(multiplier),
        contributions=contributions, groups=groups,
        complete=all(e.outcome != "unknown" for e in contributions if e.weight > 0))
