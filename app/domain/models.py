"""Versioned input, feature, rule and output contracts for PREPARE -> RANK.

Only A0 changes this module. Unknown observations are None, never fabricated zeros.
"""
from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Quality = Literal["high", "medium", "low", "missing"]
SeasonType = Literal["winter", "spring", "summer"]
RuleStatus = Literal["draft", "approved", "retired", "demo"]
Effect = Literal["incompatible", "discouraged_strong", "discouraged_mild", "neutral", "favorable"]
Mode = Literal["FULL", "AGRO_ONLY", "LIMITED", "NEEDS_INPUT", "ABSTAIN"]


def as_of_cutoff(value: date) -> datetime:
    """Date-only requests include the whole UTC day, consistently across adapters."""
    return datetime.combine(value, time.max, timezone.utc)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, validate_default=True)


class Interval(Model):
    lo: float
    mid: float
    hi: float

    @model_validator(mode="after")
    def ordered(self):
        if not self.lo <= self.mid <= self.hi:
            raise ValueError("Expected lo <= mid <= hi")
        return self

    def product(self, *others: Interval) -> Interval:
        values = [self, *others]
        if any(v.lo < 0 for v in values):
            raise ValueError("Score multipliers must be nonnegative")
        from math import prod
        return Interval(**{k: prod(getattr(v, k) for v in values) for k in ("lo", "mid", "hi")})


class ScenarioValues(Model):
    low: float
    base: float
    high: float
    source: str | None = None

    @model_validator(mode="after")
    def ordered(self):
        if not self.low <= self.base <= self.high:
            raise ValueError("Expected low <= base <= high")
        return self


class Geometry(Model):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list[Any]


class FieldInput(Model):
    field_id: str = Field(min_length=1, max_length=128)
    geometry: Geometry
    crs: str = Field(pattern=r"^EPSG:\d+$")


class TargetSeason(Model):
    harvest_year: int = Field(ge=1900, le=2200)
    include: list[SeasonType] = Field(min_length=1)


class DateRange(Model):
    from_date: date = Field(alias="from")
    to: date

    @model_validator(mode="after")
    def ordered(self):
        if self.from_date > self.to:
            raise ValueError("Planned date range is reversed")
        return self


class CropHistoryRecord(Model):
    record_id: str | None = None
    season_year: int = Field(ge=1900, le=2200, validation_alias=AliasChoices("season_year", "year"))
    crop: str = Field(min_length=1)
    variant: str | None = None
    role: Literal["main", "second_crop", "cover_crop"] = "main"
    status: Literal["harvested", "in_field", "planned", "unknown"] = "unknown"
    sowing_date: date | None = None
    harvest_date: date | None = None
    sowing_date_precision: Literal["day", "month", "season"] = "day"
    harvest_date_precision: Literal["day", "month", "season"] = "day"
    harvest_date_planned: DateRange | None = None
    yield_t_ha: float | None = Field(default=None, gt=0)
    source: Literal["farm_records", "user_memory", "platform_detected", "imported"] = "user_memory"
    reliability: Literal["high", "medium", "low"] = "low"
    available_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def ordered_dates(self):
        if self.sowing_date and self.harvest_date and self.sowing_date > self.harvest_date:
            raise ValueError("Harvest precedes sowing")
        return self


class HistoryGap(Model):
    season_year: int
    reason: str = "unknown"


class Management(Model):
    input_level: Literal["high", "low"] = "high"
    irrigation: Literal["none", "irrigated"] = "none"


class Constraints(Model):
    excluded_crops: list[str] = Field(default_factory=list)
    notes: str | None = None


class PriceBasis(Model):
    incoterm: Literal["EXW", "FCA", "CPT", "FOB", "DAP"] = "EXW"
    vat: Literal["excluded", "included"] = "excluded"


class CropCost(Model):
    value: float = Field(ge=0)
    drying_per_t: float = Field(default=0, ge=0)
    logistics_per_t: float = Field(default=0, ge=0)
    includes: list[str] = Field(default_factory=list)


class ContractPrice(Model):
    crop: str
    price: float = Field(gt=0)
    unit: Literal["t", "kg", "bu"] = "t"
    basis: Literal["EXW", "FCA", "CPT", "FOB", "DAP"] = "EXW"
    currency: str | None = None
    vat_included: bool = False
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    available_at: AwareDatetime | None = None


class EconomicsInput(Model):
    currency: Literal["UAH"] = "UAH"
    price_basis: PriceBasis = Field(default_factory=PriceBasis)
    costs_per_ha: dict[str, CropCost] = Field(default_factory=dict)
    expected_yield_t_ha: dict[str, ScenarioValues] = Field(default_factory=dict)
    contract_prices: list[ContractPrice] = Field(default_factory=list)

    @model_validator(mode="after")
    def positive_yields(self):
        if any(v.low <= 0 for v in self.expected_yield_t_ha.values()):
            raise ValueError("Yield scenarios must be positive; unknown yield is omitted")
        return self


class Options(Model):
    max_results: int = Field(default=5, ge=1, le=5)
    language: Literal["uk"] = "uk"


class RecommendationRequest(Model):
    field: FieldInput
    as_of_date: date
    target_season: TargetSeason
    crop_history: list[CropHistoryRecord] = Field(default_factory=list)
    history_gaps: list[HistoryGap] = Field(default_factory=list)
    management: Management = Field(default_factory=Management)
    constraints: Constraints = Field(default_factory=Constraints)
    economics: EconomicsInput | None = None
    options: Options = Field(default_factory=Options)


class RuleSource(Model):
    type: str = "regional_guideline"
    ref: str | None = None
    url: str | None = None
    verified_by: str | None = None
    verified_at: date | None = None


class CropVariant(Model):
    id: str
    name_uk: str
    family: str
    season_type: SeasonType
    gaez_code: str | None = None
    gaez_match: Literal["exact", "parent_crop"] = "exact"
    establishment_moisture_sensitive: bool | None = None
    commodity_spec: str | None = None
    parent_crop: str | None = None
    regions: list[str] = Field(default_factory=lambda: ["UA-53"])
    status: RuleStatus = "draft"
    source: RuleSource = Field(default_factory=RuleSource)


class SowingWindow(Model):
    crop_variant: str
    region: str
    earliest: str | None = None  # MM-DD; winter is harvest_year - 1
    latest: str | None = None
    min_lead_days: int | None = Field(default=None, ge=0)
    prep_days: int | None = Field(default=None, ge=0)
    status: RuleStatus = "draft"
    source: RuleSource = Field(default_factory=RuleSource)


class RuleTarget(Model):
    candidate: str | None = None
    candidate_family: str | None = None
    season_type: SeasonType | None = None


class RuleCondition(Model):
    type: Literal["same_crop", "same_family", "predecessor_is", "harvest_sowing_gap", "return_interval"]
    scope: Literal["same_crop", "same_family"] | None = None
    predecessor: str | None = None
    min_years_between: int | None = Field(default=None, ge=1)
    years: int | None = Field(default=None, ge=1)
    min_days: int | None = Field(default=None, ge=0)


class RotationRule(Model):
    id: str
    version: int = Field(ge=1)
    status: RuleStatus = "draft"
    regions: list[str]
    applies_to: RuleTarget
    condition: RuleCondition
    effect: Effect
    source: RuleSource = Field(default_factory=RuleSource)
    explanation_uk: str


class RankingParameters(Model):
    status: RuleStatus = "draft"
    source: RuleSource = Field(default_factory=RuleSource)
    floor_agro: float | None = Field(default=None, ge=0, le=1)
    effect_multipliers: dict[str, float | None] = Field(default_factory=dict)
    rotation_missing: Interval | None = None
    weather_multipliers: dict[str, Interval] = Field(default_factory=dict)
    weather_missing: Interval | None = None
    nowcast_horizon_days: int | None = Field(default=None, ge=0)
    weather_dry_percentile: float | None = Field(default=None, ge=0, le=100)
    weather_wet_percentile: float | None = Field(default=None, ge=0, le=100)
    near_tie_epsilon: float | None = Field(default=None, ge=0)
    price_stale_days: int | None = Field(default=None, ge=0)
    price_max_age_days: int | None = Field(default=None, ge=0)
    price_scenario_fraction: float | None = Field(default=None, ge=0, lt=1)
    robustness_cost_fraction: float | None = Field(default=None, ge=0, lt=1)


class RuleSet(Model):
    version: str
    catalog: list[CropVariant]
    sowing_windows: list[SowingWindow] = Field(default_factory=list)
    rotation: list[RotationRule] = Field(default_factory=list)
    parameters: RankingParameters = Field(default_factory=RankingParameters)
    demo: bool = False


class CellValue(Model):
    value: float | None
    area_fraction: float = Field(ge=0, le=1)
    row: int | None = None
    col: int | None = None


class GaezFieldValue(Model):
    layer_code: str
    variable: str
    unit: str
    crop_code: str | None = None
    wsim: str = "HRLM"
    period: str = "HP0120"
    clim: str = "AGERA5"
    ssp: str = "HIST"
    native_res_deg: float = Field(gt=0)
    field_area_ha: float = Field(gt=0)
    cell_area_ha_mean: float = Field(gt=0)
    field_to_cell_ratio: float = Field(gt=0)
    coverage_valid: float = Field(ge=0, le=1)
    n_cells: int = Field(ge=0)
    mean_w: float | None = None
    min: float | None = None
    max: float | None = None
    class_shares: dict[int, float] | None = None
    water_share: float | None = Field(default=None, ge=0, le=1)
    dominant_class: int | None = Field(default=None, ge=1, le=8)
    worst_class: int | None = Field(default=None, ge=1, le=8)
    per_cell: list[CellValue] = Field(default_factory=list)
    source_sha256: str
    retrieved_at: AwareDatetime
    available_at: AwareDatetime
    source_url: str | None = None

    @model_validator(mode="after")
    def valid_measurements(self):
        if self.unit == "index_0_10000":
            values = [self.mean_w, self.min, self.max]
            values += [cell.value for cell in self.per_cell]
            if any(value is not None and not 0 <= value <= 10000 for value in values):
                raise ValueError("GAEZ suitability index must lie in 0..10000")
        if self.class_shares is not None:
            if any(key not in range(1, 9) or not 0 <= value <= 1 for key, value in self.class_shares.items()):
                raise ValueError("Class shares must use land classes 1..8 and fractions 0..1")
            if sum(self.class_shares.values()) > 1.000001:
                raise ValueError("Class shares exceed total area")
        return self


class NormalizedHistory(Model):
    records: list[CropHistoryRecord] = Field(default_factory=list)
    gaps: list[HistoryGap] = Field(default_factory=list)
    current_crop: CropHistoryRecord | None = None
    occupied: bool | None = None
    quality: Quality = "missing"
    issues: list[str] = Field(default_factory=list)


class SatelliteObservation(Model):
    date: date
    acquired_at: AwareDatetime
    available_at: AwareDatetime
    valid_pixel_fraction: float = Field(ge=0, le=1)
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p90: float | None = None
    std: float | None = Field(default=None, ge=0)


class SatelliteSeries(Model):
    index: Literal["NDVI", "NDMI"] = "NDVI"
    sensor: str
    native_resolution_m: float = Field(gt=0)
    processing_version: str
    observations: list[SatelliteObservation] = Field(default_factory=list)


class SatelliteFeatures(Model):
    role: Literal["history_check"] = "history_check"
    effect_on_rank: Literal["none"] = "none"
    quality: Quality = "missing"
    obs_count_valid: int = 0
    occupied: bool | None = None
    notes: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    available_at: AwareDatetime | None = None
    sensor: str | None = None
    native_resolution_m: float | None = Field(default=None, gt=0)
    processing_version: str | None = None
    median_valid_pixel_fraction: float | None = Field(default=None, ge=0, le=1)


class WeatherRecord(Model):
    valid_time: AwareDatetime
    available_at: AwareDatetime
    issued_at: AwareDatetime | None = None
    kind: Literal["reanalysis", "forecast", "observation", "climatology"]
    provider: str
    values: dict[str, float | None]


class WeatherFeatures(Model):
    available_at: AwareDatetime | None = None
    signal: Literal["dry", "normal", "wet_ok", "unknown"] = "unknown"
    moisture_percentile: float | None = Field(default=None, ge=0, le=100)
    precipitation_percentile: float | None = Field(default=None, ge=0, le=100)
    multiplier: Interval | None = None
    quality: Quality = "missing"
    risks: list[RiskIndicator] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class MarketPrice(Model):
    price_id: str
    crop: str
    commodity_spec: str
    price_value: float = Field(gt=0)
    currency: str
    unit: Literal["t", "kg", "bu"]
    price_date: date
    published_at: AwareDatetime
    available_at: AwareDatetime | None = None
    location_type: str
    region: str
    incoterm: Literal["EXW", "FCA", "CPT", "FOB", "DAP"]
    vat_included: bool
    vat_rate: float | None = Field(default=None, ge=0, le=1)
    delivery_period: str | None = None
    quality_basis: str | None = None
    price_type: str | None = None
    source: str
    source_reliability: Literal["high", "medium", "low"]


class ExchangeRate(Model):
    date: date
    currency: str
    rate_to_UAH: float = Field(gt=0)
    source: str
    available_at: AwareDatetime | None = None


class NormalizedPrice(Model):
    base: float = Field(gt=0)
    low: float | None = None
    high: float | None = None
    currency: Literal["UAH"] = "UAH"
    unit: Literal["t"] = "t"
    basis: str
    vat: Literal["excluded", "included"] = "excluded"
    price_date: date
    age_days: int = Field(ge=0)
    price_id: str | None = None
    source: str
    transformations: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class EconomicsResult(Model):
    applied_to_rank: bool = False
    complete: bool = False
    price: NormalizedPrice | None = None
    yield_t_ha: ScenarioValues | None = None
    revenue_uah_ha: ScenarioValues | None = None
    costs_uah_ha: float | None = None
    margin_uah_ha: ScenarioValues | None = None
    issues: list[str] = Field(default_factory=list)
    transformations: list[str] = Field(default_factory=list)


class SourceError(Model):
    source: str
    code: str
    message: str


class FieldFeatureSnapshot(Model):
    snapshot_id: str
    field_id: str
    geom_hash: str
    as_of_date: date
    prepared_at: AwareDatetime
    region: str | None = None
    gaez: dict[str, list[GaezFieldValue]] = Field(default_factory=dict)
    history: NormalizedHistory = Field(default_factory=NormalizedHistory)
    weather: WeatherFeatures = Field(default_factory=WeatherFeatures)
    satellite: SatelliteFeatures = Field(default_factory=SatelliteFeatures)
    prices: list[MarketPrice] = Field(default_factory=list)
    exchange_rates: list[ExchangeRate] = Field(default_factory=list)
    costs_per_ha: dict[str, CropCost] = Field(default_factory=dict)
    source_errors: list[SourceError] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    request_hash: str
    input_hash: str | None = None
    demo: bool = False


class TraceEvent(Model):
    id: str
    code: str
    crop: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)


class Explanation(Model):
    text: str
    trace_event_id: str


class RiskIndicator(Model):
    code: str
    level: Literal["low", "moderate", "high", "unknown"]


class GateResult(Model):
    gate: str
    outcome: Literal["PASS", "EXCLUDE", "CONDITIONAL", "NOT_EVALUATED"]
    reason: str | None = None
    trace_event_id: str | None = None


class DataQualitySummary(Model):
    level: Quality = "missing"
    issues: list[str] = Field(default_factory=list)


class DataQualityReport(Model):
    overall: Quality
    by_source: dict[str, Quality] = Field(default_factory=dict)


class StabilityResult(Model):
    p_top5: float = Field(ge=0, le=1)
    rank_range: tuple[int, int]
    rank_median: float | None = None
    label: Literal["stable", "sensitive", "unstable", "unavailable"]
    main_driver: str | None = None
    scenarios_evaluated: int = Field(default=1, ge=1)


class RuleFired(Model):
    id: str
    version: int
    effect: Effect


class RotationResult(Model):
    multiplier: Interval
    rules_fired: list[RuleFired] = Field(default_factory=list)


class WeatherResult(Model):
    multiplier: Interval
    signal: str = "unknown"
    risks: list[RiskIndicator] = Field(default_factory=list)


class GaezResult(Model):
    layer: str
    crop_code: str
    match: Literal["exact", "parent_crop"]
    scenario: dict[str, str]
    suitability_index: Interval
    class_shares: dict[int, float] | None = None
    coverage_valid: float
    resolution_deg: float
    field_to_cell_ratio: float


class CandidateEvaluation(Model):
    crop: CropVariant
    gate_results: list[GateResult] = Field(default_factory=list)
    a_gaez: Interval
    m_rot: Interval
    m_wx: Interval
    agro: Interval
    tier: int | None = None
    economics: EconomicsResult | None = None
    risks: list[RiskIndicator] = Field(default_factory=list)
    data_quality: DataQualitySummary = Field(default_factory=DataQualitySummary)
    stability: StabilityResult | None = None
    trace: list[TraceEvent] = Field(default_factory=list)


class RankedCrop(Model):
    rank: int = Field(ge=1, le=5)
    crop_variant: str
    name_uk: str
    tier: int = Field(ge=1)
    status: Literal["recommended", "conditional"] = "recommended"
    agro_score: Interval
    gaez: GaezResult
    rotation: RotationResult
    weather: WeatherResult
    satellite: SatelliteFeatures
    economics: EconomicsResult | None = None
    stability: StabilityResult
    data_quality: DataQualitySummary
    assumptions: list[str] = Field(default_factory=list)
    reasons: list[Explanation] = Field(default_factory=list)
    limitations: list[Explanation] = Field(default_factory=list)
    near_tie_with: list[str] = Field(default_factory=list)


class ExcludedCrop(Model):
    crop_variant: str
    gate: str
    text: str
    trace_event_id: str


class NotEvaluatedCrop(Model):
    crop_variant: str
    reason: str
    trace_event_id: str | None = None


class InputRequest(Model):
    field: str
    why: str


class Provenance(Model):
    algorithm_version: str
    rules_version: str
    snapshot_id: str
    gaez_layers: list[dict[str, Any]] = Field(default_factory=list)
    weather: list[dict[str, Any]] = Field(default_factory=list)
    price_ids: list[str] = Field(default_factory=list)
    robustness_grid_hash: str
    source_errors: list[SourceError] = Field(default_factory=list)
    demo: bool = False


class RecommendationResult(Model):
    recommendation_id: str
    status: Literal["ok", "needs_input", "abstain", "error"]
    mode: Mode
    as_of_date: date
    computed_at: AwareDatetime
    results: list[RankedCrop] = Field(default_factory=list, max_length=5)
    excluded: list[ExcludedCrop] = Field(default_factory=list)
    below_threshold: list[NotEvaluatedCrop] = Field(default_factory=list)
    not_evaluated: list[NotEvaluatedCrop] = Field(default_factory=list)
    requests_for_input: list[InputRequest] = Field(default_factory=list)
    data_quality: DataQualityReport
    provenance: Provenance
    trace: list[TraceEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def referenced_trace(self):
        ids = {event.id for event in self.trace}
        if len(ids) != len(self.trace):
            raise ValueError("Duplicate trace event id")
        refs = [e.trace_event_id for c in self.results for e in [*c.reasons, *c.limitations]]
        refs += [c.trace_event_id for c in self.excluded]
        if any(ref not in ids for ref in refs):
            raise ValueError("Explanation references nonexistent trace event")
        return self


class JobResult(Model):
    job_id: str
    status: Literal["queued", "running", "completed", "partial", "failed"]
    snapshot_id: str | None = None
    error: str | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


WeatherFeatures.model_rebuild()
