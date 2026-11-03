"""Explicit offline-by-default runtime configuration."""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SIVAY_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./sivay.db"
    rules_path: Path = Path("rules")
    gaez_root: Path = Path("data/gaez")
    gaez_manifest: Path | None = None
    regions_path: Path | None = Path("data/regions.geojson")
    prices_path: Path | None = None
    exchange_rates_path: Path | None = None
    satellite_path: Path | None = None
    weather_path: Path | None = None
    weather_enabled: bool = False
    demo: bool = False
    ranking_profile: Literal["gaez_baseline", "personalized", "validated"] = "personalized"
    personalization_policy_path: Path = Path("rules/personalization.json")
    automatic_sources_enabled: bool = True
    automatic_weather_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    automatic_indices_ttl_seconds: int = Field(default=21600, ge=60, le=604800)
    cors_origins: list[str] = Field(default_factory=list)
    api_key: SecretStr | None = Field(default=None, min_length=32)
    job_max_attempts: int = 3
    job_lease_seconds: int = 300
