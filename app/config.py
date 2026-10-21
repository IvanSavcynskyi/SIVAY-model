"""Explicit offline-by-default runtime configuration."""
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


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
    ranking_profile: Literal["gaez_baseline", "validated"] = "gaez_baseline"
    cors_origins: list[str] = Field(default_factory=list)
    job_max_attempts: int = 3
    job_lease_seconds: int = 300
