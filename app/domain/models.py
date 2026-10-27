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
    personalization: FieldPersonalizationInput | None = None


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


class FieldWeatherInput(Model):
    """Daily point export, in Celsius, mm/day and volumetric m³/m³."""
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    resolution_m: float | None = Field(default=None, gt=0)
    max_distance_km: float = Field(default=25, gt=0, le=100)
    records: list[WeatherRecord] = Field(max_length=20000)

    @model_validator(mode="after")
    def physical_values(self):
        for record in self.records:
            values = record.values
            if any(value is not None and value < 0 for name, value in values.items()
                   if name in {"precip", "et0", "snow_depth"}):
                raise ValueError("Precipitation, ET0 and snow depth must be nonnegative")
            if any(value is not None and not 0 <= value <= 1 for name, value in values.items()
                   if name.startswith("soil_moisture")):
                raise ValueError("Soil moisture must be volumetric m3/m3 in 0..1")
            for name in ("tmin", "tmax"):
                if values.get(name) is not None and not -100 <= values[name] <= 70:
                    raise ValueError("Temperatures must use degrees Celsius")
            if (values.get("tmin") is not None and values.get("tmax") is not None
                    and values["tmin"] > values["tmax"]):
                raise ValueError("Daily minimum temperature exceeds maximum")
        return self


class FieldImageInput(Model):
    """Georeferenced GeoTIFF with explicit band identities and radiometric scale."""
    image_id: str = Field(min_length=1, max_length=128)
    data_base64: str = Field(min_length=1, max_length=14000000)
    acquired_at: AwareDatetime
    available_at: AwareDatetime
    sensor: str = Field(min_length=1)
    processing_version: str = Field(min_length=1)
    bands: dict[Literal["red", "nir", "swir", "NDVI", "NDMI", "quality"], int]
    scale: float = Field(gt=0)
    offset: float = 0
    clear_values: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_image(self):
        if self.acquired_at > self.available_at:
            raise ValueError("Image availability precedes acquisition")
        if not self.bands or any(v < 1 for v in self.bands.values()):
            raise ValueError("Image band numbers are one-based")
        if len(set(self.bands.values())) != len(self.bands):
            raise ValueError("Image band identities must be distinct")
        if "quality" not in self.bands or not self.clear_values:
            raise ValueError("A quality-mask band and clear_values are required")
        if not ({"NDVI", "NDMI"} & self.bands.keys() or
                ("nir" in self.bands and {"red", "swir"} & self.bands.keys())):
            raise ValueError("Provide index bands or NIR with red/SWIR; RGB alone has no spectral index")
        return self


class PersonalizationRule(Model):
    id: str = Field(min_length=1)
    crops: list[str] = Field(min_length=1)
    metric: Literal["NDVI.mean", "NDMI.mean", "NDVI.delta", "NDMI.delta",
                    "weather.precip_7d", "weather.precip_30d", "weather.balance_30d",
                    "weather.soil_moisture", "weather.tmin_7d", "weather.tmax_7d",
                    "forecast.tmin_7d", "forecast.tmax_7d", "forecast.precip_7d"]
    operator: Literal["lt", "gt"]
    threshold: float
    priority: Literal[1, 2, 3] = 1
    horizon_days: int = Field(default=14, ge=0, le=30)
    explanation_uk: str = Field(min_length=1)
    source: str = Field(min_length=1)


class PersonalizationPolicy(Model):
    version: str = Field(min_length=1)
    source: str = Field(min_length=1)
    # A farm policy, explicitly separate from the approved agronomy rule bundle.
    status: Literal["experimental", "farm_configured"] = "farm_configured"
    avoid_same_crop_predecessor: bool = True
    min_return_years: dict[str, int] = Field(default_factory=dict)
    avoid_predecessors: dict[str, list[str]] = Field(default_factory=dict)
    rules: list[PersonalizationRule] = Field(default_factory=list, max_length=100)
    min_valid_fraction: float = Field(default=.8, gt=0, le=1)
    max_index_age_days: int = Field(default=30, ge=1, le=90)
    max_weather_age_days: int = Field(default=3, ge=0, le=7)

    @model_validator(mode="after")
    def consistent_policy(self):
        if any(not 1 <= years <= 30 for years in self.min_return_years.values()):
            raise ValueError("Return intervals must be 1..30 years")
        if len({rule.id for rule in self.rules}) != len(self.rules):
            raise ValueError("Duplicate personalization rule id")
        return self


class FieldPersonalizationInput(Model):
    indices: list[SatelliteSeries] = Field(default_factory=list, max_length=2)
    weather: FieldWeatherInput | None = None
    policy: PersonalizationPolicy | None = None
    sowing_dates: dict[str, date] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_indices(self):
        if len({series.index for series in self.indices}) != len(self.indices):
            raise ValueError("Only one series per index may be supplied")
        return self


class FieldSignals(Model):
    metrics: dict[str, float] = Field(default_factory=dict)
    metric_dates: dict[str, date] = Field(default_factory=dict)
    metric_sources: dict[str, list[str]] = Field(default_factory=dict)
    indices: dict[str, dict[str, Any]] = Field(default_factory=dict)
    images: list[dict[str, Any]] = Field(default_factory=list)
    weather: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    policy: PersonalizationPolicy | None = None
    source_availability: dict[str, AwareDatetime] = Field(default_factory=dict)


class PersonalizationAssessment(Model):
    rule_id: str
    source: str
    outcome: Literal["risk", "pass", "unknown", "not_applicable"]
    priority: int = Field(default=0, ge=0, le=3)
    explanation_uk: str
    facts: dict[str, Any] = Field(default_factory=dict)


class PersonalizationResult(Model):
    policy_version: str
    policy_status: Literal["experimental", "farm_configured"]
    risk_priority: int = Field(ge=0, le=3)
    assessments: list[PersonalizationAssessment]
    metrics: dict[str, float] = Field(default_factory=dict)
    sources: dict[str, list[str]] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    # GAEZ remains a suitability index; risks change order, never fabricate yield.
    ranking_method: Literal["risk_priority_then_gaez"] = "risk_priority_then_gaez"


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
    field_signals: FieldSignals = Field(default_factory=FieldSignals)
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
    personalization: PersonalizationResult | None = None


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
RecommendationRequest.model_rebuild()
