from copy import deepcopy
from datetime import date, datetime, timezone

import pytest

from app.adapters.prices import filter_exchange_rates, filter_prices, load_prices_csv
from app.domain import (
    ContractPrice, CropHistoryRecord, CropVariant, ExchangeRate, FieldFeatureSnapshot, MarketPrice, RankingParameters,
    RecommendationRequest, RuleSet,
)
from app.features.economics import compute_economics


def inputs():
    crop = CropVariant(id="wheat_winter", name_uk="Пшениця озима", family="cereals",
                       season_type="winter", commodity_spec="grain_class3", parent_crop="wheat")
    request = RecommendationRequest.model_validate({
        "field": {"field_id": "f1", "crs": "EPSG:4326", "geometry": {
            "type": "Polygon", "coordinates": [[[34, 49], [34.01, 49], [34, 49.01], [34, 49]]]}},
        "as_of_date": "2026-09-01", "target_season": {"harvest_year": 2027, "include": ["winter"]},
        "economics": {"costs_per_ha": {"wheat_winter": {"value": 30000}},
                      "expected_yield_t_ha": {"wheat_winter": {"low": 4.5, "base": 5.5, "high": 6.3}}},
    })
    snapshot = FieldFeatureSnapshot(
        snapshot_id="s1", field_id="f1", geom_hash="g", request_hash="r", region="UA-53",
        as_of_date=request.as_of_date, prepared_at=datetime.now(timezone.utc), prices=[market()],
    )
    rules = RuleSet(version="test-reviewed", catalog=[crop], parameters=RankingParameters(
        status="approved", price_stale_days=30, price_max_age_days=90, price_scenario_fraction=.15,
    ))
    return crop, request, snapshot, rules


def market(**updates):
    return MarketPrice.model_validate({
        "price_id": "p1", "crop": "wheat", "commodity_spec": "grain_class3", "price_value": 8500,
        "currency": "UAH", "unit": "t", "price_date": "2026-08-28",
        "published_at": "2026-08-29T06:00:00Z", "location_type": "farm", "region": "UA-53",
        "incoterm": "EXW", "vat_included": False, "source": "test", "source_reliability": "high",
        **updates,
    })


def test_complete_economics_matches_worked_example_and_never_claims_rank_application():
    args = inputs()
    before = deepcopy(args)
    result = compute_economics(*args)
    assert result.complete and not result.applied_to_rank
    assert result.price.low == 7225
    assert result.price.high == 9775
    assert result.revenue_uah_ha.base == 46750
    assert result.margin_uah_ha.base == 16750
    assert result.margin_uah_ha.low == 2512.5
    assert result.transformations and "p1" in " ".join(result.transformations)
    assert args == before


def test_without_costs_revenue_remains_informational():
    crop, request, snapshot, rules = inputs()
    request.economics.costs_per_ha.clear()
    result = compute_economics(crop, request, snapshot, rules)
    assert result.revenue_uah_ha.base == 46750
    assert result.margin_uah_ha is None
    assert not result.complete and not result.applied_to_rank
    assert "ECON_COST_MISSING" in result.issues


def test_fixed_contract_wins_over_market_and_needs_no_scenario_policy():
    crop, request, snapshot, rules = inputs()
    request.economics.contract_prices = [ContractPrice(crop=crop.id, price=9000)]
    rules.parameters.status = "draft"
    result = compute_economics(crop, request, snapshot, rules)
    assert result.complete
    assert result.price.low == result.price.base == result.price.high == 9000
    assert result.price.source == "user_contract"


@pytest.mark.parametrize("change,expected", [
    ({"unit": "bu"}, "ECON_BUSHEL_FACTOR_MISSING"),
    ({"vat_included": True}, "ECON_VAT_RATE_MISSING"),
    ({"currency": "USD"}, "ECON_FX_MISSING"),
    ({"incoterm": "CPT"}, "ECON_BASIS_MISMATCH"),
    ({"crop": "unrelated"}, "ECON_PRICE_MISSING"),
    ({"commodity_spec": "feed"}, "ECON_PRICE_MISSING"),
    ({"region": "UA-30"}, "ECON_PRICE_MISSING"),
    ({"delivery_period": "2027-07"}, "ECON_PRICE_MISSING"),
])
def test_missing_physical_or_commodity_assumptions_are_not_guessed(change, expected):
    crop, request, snapshot, rules = inputs()
    snapshot.prices = [market(**change)]
    result = compute_economics(crop, request, snapshot, rules)
    assert expected in result.issues
    assert not result.complete


def test_unit_fx_vat_and_basis_transformation_and_logistics_counted_once():
    crop, request, snapshot, rules = inputs()
    snapshot.prices = [market(price_value=.3, currency="USD", unit="kg", vat_included=True,
                             vat_rate=.2, incoterm="CPT")]
    snapshot.exchange_rates = [ExchangeRate(
        date=date(2026, 8, 28), currency="USD", rate_to_UAH=40, source="test-fx",
        available_at=datetime(2026, 8, 28, 10, tzinfo=timezone.utc),
    )]
    request.economics.costs_per_ha[crop.id].logistics_per_t = 1000
    request.economics.costs_per_ha[crop.id].drying_per_t = 100
    result = compute_economics(crop, request, snapshot, rules)
    assert result.complete
    assert result.price.base == 9000  # .3/kg * 1000 * 40 / 1.2 - 1000
    assert result.costs_uah_ha == 30550  # logistics already reflected in EXW price
    assert result.margin_uah_ha.base == 18950
    assert "test-fx" in " ".join(result.transformations)


def test_embedded_costs_are_not_added_twice_and_ambiguous_basis_conversion_is_rejected():
    crop, request, snapshot, rules = inputs()
    cost = request.economics.costs_per_ha[crop.id]
    cost.includes = ["drying", "logistics"]
    cost.logistics_per_t = 1000
    cost.drying_per_t = 100
    assert compute_economics(crop, request, snapshot, rules).costs_uah_ha == 30000
    snapshot.prices = [market(incoterm="CPT")]
    result = compute_economics(crop, request, snapshot, rules)
    assert "ECON_LOGISTICS_ALREADY_INCLUDED" in result.issues
    assert not result.complete


def test_freshness_and_unapproved_scenario_policies():
    crop, request, snapshot, rules = inputs()
    snapshot.prices = [market(price_date="2026-07-20", published_at="2026-07-20T12:00:00Z")]
    result = compute_economics(crop, request, snapshot, rules)
    assert result.price.low == 8500 * .7
    assert result.price.high == 8500 * 1.3
    assert "ECON_PRICE_STALE" in result.issues
    snapshot.prices = [market(price_date="2026-05-01", published_at="2026-05-01T12:00:00Z")]
    assert "ECON_PRICE_TOO_OLD" in compute_economics(crop, request, snapshot, rules).issues
    snapshot.prices = [market()]
    rules.parameters.price_scenario_fraction = None
    result = compute_economics(crop, request, snapshot, rules)
    assert result.price.base == 8500 and result.price.low is None
    assert not result.complete and "ECON_PRICE_SCENARIOS_UNAPPROVED" in result.issues
    rules.parameters.status = "draft"
    assert "ECON_FRESHNESS_POLICY_UNAPPROVED" in compute_economics(crop, request, snapshot, rules).issues


def test_future_data_is_filtered_even_when_supplied_directly_in_snapshot():
    crop, request, snapshot, rules = inputs()
    for change in [
        {"price_date": "2026-09-02"},
        {"published_at": "2026-09-02T00:00:00Z"},
        {"available_at": "2026-09-02T00:00:00Z"},
    ]:
        snapshot.prices = [market(**change)]
        assert not compute_economics(crop, request, snapshot, rules).complete
    future_fx = ExchangeRate(date=date(2026, 8, 28), currency="USD", rate_to_UAH=40,
                             source="test", available_at="2026-09-02T00:00:00Z")
    assert filter_exchange_rates([future_fx], request.as_of_date) == []
    assert filter_exchange_rates([future_fx.model_copy(update={"available_at": None})], request.as_of_date) == []
    assert filter_prices([market()], datetime(2026, 8, 29, 5, tzinfo=timezone.utc)) == []


def test_user_yields_override_history_and_two_distinct_reliable_years_are_required():
    crop, request, snapshot, rules = inputs()
    snapshot.history.records = [
        CropHistoryRecord(record_id=str(year), crop=crop.id, season_year=year,
                          status="harvested", reliability="high", yield_t_ha=value)
        for year, value in [(2024, 2), (2025, 4)]
    ]
    result = compute_economics(crop, request, snapshot, rules)
    assert result.yield_t_ha.base == 5.5 and result.yield_t_ha.source == "user"
    request.economics.expected_yield_t_ha.clear()
    result = compute_economics(crop, request, snapshot, rules)
    assert result.yield_t_ha.low == 2 and result.yield_t_ha.base == 3 and result.yield_t_ha.high == 4
    snapshot.history.records[1].reliability = "low"
    assert "ECON_YIELD_MISSING" in compute_economics(crop, request, snapshot, rules).issues


def test_no_yield_is_derived_from_gaez_or_from_current_crop():
    crop, request, snapshot, rules = inputs()
    request.economics.expected_yield_t_ha.clear()
    snapshot.history.records = []
    assert not compute_economics(crop, request, snapshot, rules).complete


def test_price_csv_is_validated_and_filters_after_publication(tmp_path):
    row = market().model_dump(mode="json")
    import csv
    path = tmp_path / "prices.csv"
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    assert [item.price_id for item in load_prices_csv(path, date(2026, 9, 1))] == ["p1"]
    assert load_prices_csv(path, date(2026, 8, 28)) == []
    with path.open("a", newline="") as handle:
        csv.DictWriter(handle, fieldnames=list(row)).writerow(row)
    with pytest.raises(ValueError, match="Duplicate price_id"):
        load_prices_csv(path, date(2026, 9, 1))
