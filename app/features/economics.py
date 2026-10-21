"""Pure, conservative scenario economics; ranking owns applied_to_rank.

MVP supports fixed user contracts and local spot quotes. Forward selection needs
an explicit target harvest month, which the current request contract does not
provide. Bushel conversion and crop aliases require reviewed source data; none
are inferred here. FX records need available_at in addition to the rate date.
"""
from __future__ import annotations

from statistics import median

from app.adapters.prices import filter_exchange_rates, filter_prices
from app.domain import (
    ContractPrice, CropCost, CropVariant, EconomicsResult, FieldFeatureSnapshot,
    MarketPrice, NormalizedPrice, RecommendationRequest, RuleSet, ScenarioValues,
    as_of_cutoff,
)


def _yield(crop: CropVariant, request: RecommendationRequest,
           snapshot: FieldFeatureSnapshot) -> ScenarioValues | None:
    explicit = request.economics.expected_yield_t_ha.get(crop.id)
    if explicit is not None:
        return explicit.model_copy(update={"source": "user"}, deep=True)
    cutoff = as_of_cutoff(request.as_of_date)
    by_year = {}
    for row in snapshot.history.records:
        exact_crop = row.crop == crop.id or (
            crop.parent_crop is not None and row.crop == crop.parent_crop
            and row.variant in {crop.id, crop.season_type}
        )
        if not (exact_crop and row.reliability == "high" and row.role == "main"
                and row.status == "harvested" and row.yield_t_ha is not None
                and row.season_year <= request.as_of_date.year
                and (row.harvest_date is None or row.harvest_date <= request.as_of_date)
                and (row.available_at is None or row.available_at <= cutoff)):
            continue
        by_year.setdefault(row.season_year, []).append(row)
    # Multiple imported copies cannot turn one observed year into two years.
    usable = [rows[0] for rows in by_year.values()
              if len({row.yield_t_ha for row in rows}) == 1]
    if len(usable) < 2:
        return None
    values = [row.yield_t_ha for row in usable]
    return ScenarioValues(low=min(values), base=median(values), high=max(values),
                          source="field_history:" + ",".join(sorted(
                              row.record_id or str(row.season_year) for row in usable)))


def _normalize(
    row: MarketPrice | ContractPrice, request: RecommendationRequest,
    snapshot: FieldFeatureSnapshot, cost: CropCost | None,
) -> tuple[NormalizedPrice | None, bool, list[str]]:
    contract = isinstance(row, ContractPrice)
    econ = request.economics
    value = row.price if contract else row.price_value
    transforms = []
    issues = []
    day = request.as_of_date if contract else row.price_date
    source = "user_contract" if contract else row.source
    currency = (row.currency or econ.currency) if contract else row.currency
    if row.unit == "bu":
        return None, False, ["ECON_BUSHEL_FACTOR_MISSING"]
    if row.unit == "kg":
        value *= 1000
        transforms.append("unit: kg→t; price × 1000")
    else:
        transforms.append("unit: t (unchanged)")
    if currency != econ.currency:
        rates = [rate for rate in filter_exchange_rates(snapshot.exchange_rates, request.as_of_date)
                 if rate.currency == currency and rate.date == day]
        if not rates:
            return None, False, ["ECON_FX_MISSING"]
        latest = max(rate.available_at for rate in rates)
        rates = [rate for rate in rates if rate.available_at == latest]
        if len({rate.rate_to_UAH for rate in rates}) != 1:
            return None, False, ["ECON_FX_AMBIGUOUS"]
        rate = sorted(rates, key=lambda rate: rate.source)[0]
        value *= rate.rate_to_UAH
        transforms.append(f"currency: {currency}→UAH × {rate.rate_to_UAH}; "
                          f"date={rate.date}; source={rate.source}; available_at={rate.available_at.isoformat()}")
    else:
        transforms.append("currency: UAH (unchanged)")
    target_vat = econ.price_basis.vat
    if row.vat_included != (target_vat == "included"):
        if row.vat_rate is None:
            return None, False, ["ECON_VAT_RATE_MISSING"]
        if row.vat_included:
            value /= 1 + row.vat_rate
            transforms.append(f"VAT: included→excluded ÷ {1 + row.vat_rate}")
        else:
            value *= 1 + row.vat_rate
            transforms.append(f"VAT: excluded→included × {1 + row.vat_rate}")
    else:
        transforms.append(f"VAT: {target_vat} (unchanged)")
    basis = row.basis if contract else row.incoterm
    consumed_logistics = False
    if basis != econ.price_basis.incoterm:
        if (econ.price_basis.incoterm != "EXW" or basis not in {"CPT", "FOB", "DAP", "FCA"}
                or cost is None or cost.logistics_per_t <= 0):
            return None, False, ["ECON_BASIS_MISMATCH"]
        if "logistics" in cost.includes:
            return None, False, ["ECON_LOGISTICS_ALREADY_INCLUDED"]
        value -= cost.logistics_per_t
        consumed_logistics = True
        transforms.append(f"basis: {basis}→EXW; price − {cost.logistics_per_t} UAH/t "
                          "user logistics; logistics excluded from additional costs")
    else:
        transforms.append(f"basis: {basis} (unchanged)")
    if value <= 0:
        return None, False, ["ECON_NORMALIZED_PRICE_NONPOSITIVE"]
    notes = []
    if not contract:
        transforms.append(f"market: price_id={row.price_id}; published_at={row.published_at.isoformat()}; "
                          f"available_at={(row.available_at or row.published_at).isoformat()}")
        notes.append("Поточна ціна, не гарантована на час збирання.")
        if row.quality_basis:
            notes.append(f"Якість джерела: {row.quality_basis}; сумісність потребує підтвердження.")
            issues.append("ECON_QUALITY_BASIS_UNVERIFIED")
    return NormalizedPrice(
        base=value, basis=econ.price_basis.incoterm, vat=target_vat, price_date=day,
        age_days=(request.as_of_date - day).days, source=source,
        price_id=None if contract else row.price_id, transformations=transforms, notes=notes,
    ), consumed_logistics, issues


def compute_economics(
    crop: CropVariant, request: RecommendationRequest,
    snapshot: FieldFeatureSnapshot, rules: RuleSet,
) -> EconomicsResult:
    """Use only cutoff-safe observations and explicit/reviewed numeric assumptions."""
    result = EconomicsResult()
    if request.economics is None:
        result.issues.append("ECON_INPUT_MISSING")
        return result
    if snapshot.as_of_date != request.as_of_date:
        result.issues.append("ECON_SNAPSHOT_AS_OF_MISMATCH")
        return result
    econ = request.economics
    cost = econ.costs_per_ha.get(crop.id)
    result.yield_t_ha = _yield(crop, request, snapshot)
    if result.yield_t_ha is None:
        result.issues.append("ECON_YIELD_MISSING")
    else:
        result.transformations.append(f"yield_t_ha: source={result.yield_t_ha.source}; "
                                      f"{result.yield_t_ha.low}/{result.yield_t_ha.base}/{result.yield_t_ha.high}")
    if cost is None:
        result.issues.append("ECON_COST_MISSING")
    params = rules.parameters
    reviewed = params.status == "approved" or (rules.demo and params.status == "demo")
    contracts = [row for row in econ.contract_prices if row.crop == crop.id and (
        row.available_at is None or row.available_at <= as_of_cutoff(request.as_of_date))]
    contract = bool(contracts)
    consumed_logistics = False
    if contracts:
        if len({row.model_dump_json() for row in contracts}) > 1:
            result.issues.append("ECON_CONTRACT_AMBIGUOUS")
            return result
        result.price, consumed_logistics, issues = _normalize(contracts[0], request, snapshot, cost)
        result.issues.extend(issues)
    else:
        rows = [row for row in filter_prices(snapshot.prices, request.as_of_date)
                if row.crop in {crop.id, crop.parent_crop}
                and crop.commodity_spec is not None and row.commodity_spec == crop.commodity_spec
                and snapshot.region is not None
                and (row.region == snapshot.region or (row.region == "UA" and
                     (snapshot.region == "UA" or snapshot.region.startswith("UA-"))))
                and row.delivery_period in {None, "spot"}]
        if not rows:
            result.issues.append("ECON_PRICE_MISSING")
        elif (not reviewed or params.price_max_age_days is None
              or params.price_stale_days is None
              or params.price_stale_days > params.price_max_age_days):
            result.issues.append("ECON_FRESHNESS_POLICY_UNAPPROVED")
            # A quote can be displayed with its date without judging freshness.
            # It never receives scenarios or economic ranking eligibility.
            rows.sort(key=lambda row: (-row.price_date.toordinal(), -row.published_at.timestamp(), row.price_id))
            for row in rows:
                price, _, issues = _normalize(row, request, snapshot, cost)
                if price is not None:
                    result.price = price
                    result.issues.extend(issues)
                    result.transformations.extend(price.transformations)
                    price.notes.append("Довідкова ціна: політика свіжості не затверджена; у рейтингу не використовується.")
                    break
            return result
        else:
            rows.sort(key=lambda row: (-row.price_date.toordinal(), -row.published_at.timestamp(), row.price_id))
            rejected = []
            for row in rows:
                if (request.as_of_date - row.price_date).days > params.price_max_age_days:
                    rejected.append("ECON_PRICE_TOO_OLD")
                    continue
                price, consumed, issues = _normalize(row, request, snapshot, cost)
                if price is not None:
                    result.price = price
                    consumed_logistics = consumed
                    result.issues.extend(issues)
                    break
                rejected.extend(issues)
            if result.price is None:
                result.issues.extend(sorted(set(rejected)))
    if result.price is None:
        return result
    price = result.price
    if contract:
        price.low = price.high = price.base
        price.transformations.append("scenario: fixed user contract; low=base=high")
    elif not reviewed or params.price_scenario_fraction is None:
        result.issues.append("ECON_PRICE_SCENARIOS_UNAPPROVED")
    else:
        width = params.price_scenario_fraction
        if price.age_days > params.price_stale_days:
            width *= 2
            result.issues.append("ECON_PRICE_STALE")
            price.notes.append("Застаріла ціна: умовний діапазон розширено вдвічі.")
        if width >= 1:
            result.issues.append("ECON_PRICE_SCENARIOS_INVALID")
        else:
            price.low, price.high = price.base * (1 - width), price.base * (1 + width)
            price.notes.append("Умовний конфігурований діапазон ціни; не прогноз.")
            price.transformations.append(f"scenario: base × (1 ± {width}); "
                                         f"rules={rules.version}; source={params.source.ref}; "
                                         f"status={params.status}")
    result.transformations.extend(price.transformations)
    if price.low is None or price.high is None or result.yield_t_ha is None:
        return result
    yields = result.yield_t_ha
    revenue = {name: getattr(price, name)
               * getattr(yields, name) for name in ("low", "base", "high")}
    result.revenue_uah_ha = ScenarioValues(**revenue)
    result.transformations.append("revenue scenarios: price_s UAH/t × yield_s t/ha")
    if cost is None:
        return result
    drying = 0 if "drying" in cost.includes else cost.drying_per_t
    logistics = 0 if consumed_logistics or "logistics" in cost.includes else cost.logistics_per_t
    variable = drying + logistics
    costs = {name: cost.value + variable * getattr(yields, name) for name in revenue}
    margins = {name: revenue[name] - costs[name] for name in revenue}
    if not margins["low"] <= margins["base"] <= margins["high"]:
        result.issues.append("ECON_SCENARIO_ORDER_INVALID")
        return result
    result.costs_uah_ha = costs["base"]
    result.margin_uah_ha = ScenarioValues(**margins)
    result.transformations.append(
        f"cost_s: user {cost.value} UAH/ha + yield_s × ({drying} drying + {logistics} logistics) UAH/t; "
        f"includes={cost.includes}; costs(low/base/high)={costs['low']}/{costs['base']}/{costs['high']}")
    result.transformations.append("margin scenarios: revenue_s − cost_s; not profit")
    result.complete = True
    return result
