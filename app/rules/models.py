"""Review schemas, intentionally distinct from the runtime domain contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Status = Literal["draft", "approved", "retired", "demo"]
Effect = Literal["incompatible", "discouraged_strong", "discouraged_mild", "neutral", "favorable"]
Season = Literal["winter", "spring", "summer"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Source(StrictModel):
    type: str
    ref: str | None = None
    url: str | None = None
    verified_by: str | None = None
    verified_at: date | None = None

    @field_validator("type", "ref", "url", "verified_by")
    @classmethod
    def no_empty_text(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("Use null for missing source information")
        return value


class Reviewed(StrictModel):
    status: Status = "draft"
    source: Source

    @model_validator(mode="after")
    def signed_approval(self):
        if self.status == "approved":
            if not (self.source.ref or self.source.url):
                raise ValueError("approved records require a source reference or URL")
            if not self.source.verified_by or self.source.verified_at is None:
                raise ValueError("approved records require verified_by and verified_at")
        return self


class CatalogCrop(Reviewed):
    id: str
    name_uk: str
    family: str
    season_type: Season
    supported_regions: list[str] = Field(min_length=1)
    gaez_code: str | None
    gaez_match: Literal["exact", "parent_crop"] | None
    establishment_moisture_sensitive: bool | None = None
    commodity_spec: str | None = None

    @model_validator(mode="after")
    def mapping_and_approval(self):
        if (self.gaez_code is None) != (self.gaez_match is None):
            raise ValueError("GAEZ code and match must both be provided or both null")
        if self.status == "approved" and self.establishment_moisture_sensitive is None:
            raise ValueError("approved catalog requires agronomist-reviewed moisture sensitivity")
        return self


def month_day(value: str | None) -> str | None:
    if value is not None:
        try:
            parsed = datetime.strptime(value, "%m-%d")
        except ValueError as exc:
            raise ValueError("Expected a valid calendar date in MM-DD format") from exc
        if parsed.strftime("%m-%d") != value:
            raise ValueError("Expected zero-padded MM-DD format")
    return value


class SowingWindow(Reviewed):
    region: str
    crop_variant: str
    earliest: str | None = None
    latest: str | None = None
    optimal_from: str | None = None
    optimal_to: str | None = None
    min_prep_days: int | None = Field(default=None, ge=0)
    min_lead_days: int | None = Field(default=None, ge=0)
    version: int = Field(ge=1)

    _dates = field_validator("earliest", "latest", "optimal_from", "optimal_to")(month_day)

    @model_validator(mode="after")
    def complete_approved_window(self):
        fields = (self.earliest, self.latest, self.optimal_from, self.optimal_to,
                  self.min_prep_days, self.min_lead_days)
        if self.status == "approved" and any(value is None for value in fields):
            raise ValueError("approved windows require all dates, prep days and lead days")
        if all(value is not None for value in fields[:4]):
            def ordinal(value):
                return datetime.strptime("2000-" + value, "%Y-%m-%d").timetuple().tm_yday
            start = ordinal(self.earliest)
            offsets = [(ordinal(value) - start) % 366 for value in
                       (self.optimal_from, self.optimal_to, self.latest)]
            if not offsets[0] <= offsets[1] <= offsets[2]:
                raise ValueError("Optimal dates must lie in order inside the sowing window")
        return self


class RuleTarget(StrictModel):
    candidate: str | None = None
    candidate_family: str | None = None
    season_type: Season | None = None

    @model_validator(mode="after")
    def explicit_target(self):
        if bool(self.candidate) == bool(self.candidate_family):
            raise ValueError("Specify exactly one candidate or candidate_family")
        return self


class RuleCondition(StrictModel):
    type: Literal["same_crop", "same_family", "predecessor_is", "harvest_sowing_gap", "return_interval"]
    scope: Literal["same_crop", "same_family"] | None = None
    predecessor: str | None = None
    min_years_between: int | None = Field(default=None, ge=1)
    min_days_between: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def predecessor_required(self):
        if self.type == "predecessor_is" and not self.predecessor:
            raise ValueError("predecessor_is requires predecessor")
        return self


class RotationRule(Reviewed):
    id: str
    version: int = Field(ge=1)
    regions: list[str] = Field(min_length=1)
    applies_to: RuleTarget
    condition: RuleCondition
    effect: Effect
    explanation_uk: str

    @model_validator(mode="after")
    def complete_condition(self):
        if self.status == "approved":
            if self.condition.type == "return_interval" and (
                self.condition.scope is None or self.condition.min_years_between is None
            ):
                raise ValueError("approved return_interval requires scope and min_years_between")
            if self.condition.type == "harvest_sowing_gap" and self.condition.min_days_between is None:
                raise ValueError("approved harvest_sowing_gap requires min_days_between")
        return self


class Multiplier(Reviewed):
    id: str
    category: Literal["rotation", "weather", "threshold", "ranking"]
    value: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    lo: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    mid: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)
    hi: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)

    @model_validator(mode="after")
    def coherent_value(self):
        allowed = {
            "rotation": {"favorable", "neutral", "discouraged_mild", "discouraged_strong", "missing"},
            "weather": {"dry", "normal", "wet_ok", "missing"},
            "threshold": {"floor_agro"},
            "ranking": {"nowcast_horizon_days", "near_tie_epsilon", "price_stale_days", "price_max_age_days", "price_scenario_fraction", "robustness_cost_fraction", "weather_dry_percentile", "weather_wet_percentile"},
        }
        if self.id not in {f"{self.category}.{name}" for name in allowed[self.category]}:
            raise ValueError("Unknown parameter ID or category mismatch")
        interval = (self.lo, self.mid, self.hi)
        if any(v is not None for v in interval):
            if any(v is None for v in interval) or self.value is not None:
                raise ValueError("Use either a scalar or a complete interval")
            if not self.lo <= self.mid <= self.hi:
                raise ValueError("Multiplier interval must satisfy lo <= mid <= hi")
        if self.status == "approved" and self.value is None and self.lo is None:
            raise ValueError("approved parameter requires a numeric value")
        if self.category == "rotation" and self.id != "rotation.missing" and self.lo is not None:
            raise ValueError("Rotation effects require scalar multipliers")
        if (self.category == "weather" or self.id == "rotation.missing") and self.value is not None:
            raise ValueError("Weather and missing-history parameters require an interval")
        if self.category in {"ranking", "threshold"} and self.lo is not None:
            raise ValueError("Ranking and threshold parameters require a scalar")
        if self.category == "threshold" and self.value is not None and self.value > 1:
            raise ValueError("Agro thresholds must lie in [0, 1]")
        if self.category in {"rotation", "weather"} and self.value is not None and self.value > 2:
            raise ValueError("Agronomic multipliers must lie in [0, 2]")
        return self


class OraclePair(Reviewed):
    id: str
    region: str
    predecessor: str
    candidate: str
    expected_effect: Effect | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def reviewed_verdict(self):
        if self.status == "approved" and self.expected_effect is None:
            raise ValueError("approved oracle rows require a human verdict")
        return self
