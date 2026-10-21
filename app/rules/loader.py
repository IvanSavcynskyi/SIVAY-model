"""Load YAML/CSV rule bundles with deterministic content-based versions."""

from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import yaml
from pydantic import Field

from app.domain import models as domain

from .models import CatalogCrop, Multiplier, OraclePair, RotationRule, SowingWindow, Status, StrictModel


class Manifest(StrictModel):
    version: str
    status: Status
    regions: list[str] = Field(min_length=1)
    description: str


class RulesBundle(StrictModel):
    version: str
    content_hash: str
    manifest: Manifest
    catalog: list[CatalogCrop]
    sowing_windows: list[SowingWindow]
    rotation: list[RotationRule]
    multipliers: list[Multiplier]
    oracle: list[OraclePair]

    @property
    def active_catalog(self) -> list[CatalogCrop]:
        return [crop for crop in self.catalog if crop.status == "approved"]

    @property
    def active_windows(self) -> list[SowingWindow]:
        return [window for window in self.sowing_windows if window.status == "approved"]

    @property
    def active_rotation(self) -> list[RotationRule]:
        return [rule for rule in self.rotation if rule.status == "approved"]

    @property
    def active_multipliers(self) -> dict[str, Multiplier]:
        return {parameter.id: parameter for parameter in self.multipliers if parameter.status == "approved"}

    def readiness_issues(self, region: str) -> list[str]:
        issues = []
        catalog = [crop for crop in self.active_catalog if region in crop.supported_regions]
        if not catalog:
            issues.append("NO_APPROVED_CATALOG")
        windows = {window.crop_variant for window in self.active_windows if window.region == region}
        if any(crop.id not in windows for crop in catalog) or not windows:
            issues.append("NO_APPROVED_SOWING_WINDOWS")
        if not self.active_multipliers:
            issues.append("NO_APPROVED_MULTIPLIERS")
        return issues


def _yaml(path: Path):
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def _csv(path: Path):
    with path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            record = {key: value if value != "" else None for key, value in row.items()}
            record["source"] = {
                "type": record.pop("source_type"),
                "ref": record.pop("source_ref"),
                "url": record.pop("source_url"),
                "verified_by": record.pop("verified_by"),
                "verified_at": record.pop("verified_at"),
            }
            yield record


def _unique(records, key, description):
    seen = set()
    for record in records:
        value = key(record)
        if value in seen:
            raise ValueError(f"Duplicate {description}: {value}")
        seen.add(value)


def load_rule_bundle(root: str | Path = "rules", *, allow_demo: bool = False) -> RulesBundle:
    """Validate review data. Only ``active_*`` properties are eligible for production.

    The default rejects demo data, even when one individual record has demo status.
    ``allow_demo`` permits fixture inspection; it never upgrades demo to approved.
    """
    root = Path(root)
    manifest = Manifest.model_validate(_yaml(root / "manifest.yaml"))
    catalog = [CatalogCrop.model_validate(value) for value in _yaml(root / "catalog.yaml")]
    windows = [SowingWindow.model_validate(value) for path in sorted((root / "sowing_windows").glob("*.csv")) for value in _csv(path)]
    rotation = [RotationRule.model_validate(value) for path in sorted((root / "rotation").rglob("*.yaml")) for value in (_yaml(path) or [])]
    multipliers = [Multiplier.model_validate(value) for value in _yaml(root / "multipliers.yaml")]
    oracle = [OraclePair.model_validate(value) for value in _csv(root / "oracle.csv")]
    records = catalog + windows + rotation + multipliers + oracle
    if not allow_demo and (manifest.status == "demo" or any(record.status == "demo" for record in records)):
        raise ValueError("Demo rules cannot be loaded by the production loader")
    _unique(catalog, lambda record: record.id, "catalog ID")
    _unique(windows, lambda record: (record.region, record.crop_variant), "sowing window")
    _unique(rotation, lambda record: record.id, "rotation rule ID")
    _unique(multipliers, lambda record: record.id, "multiplier ID")
    _unique(oracle, lambda record: record.id, "oracle ID")
    crop_ids = {crop.id for crop in catalog}
    families = {crop.family for crop in catalog}
    for crop in catalog:
        if not set(crop.supported_regions) <= set(manifest.regions):
            raise ValueError(f"Unknown catalog region: {crop.id}")
    for window in windows:
        if window.crop_variant not in crop_ids or window.region not in manifest.regions:
            raise ValueError(f"Unknown crop or region in window: {window.crop_variant}")
    for rule in rotation:
        if not set(rule.regions) <= set(manifest.regions):
            raise ValueError(f"Unknown rule region: {rule.id}")
        target = rule.applies_to
        if target.candidate and target.candidate not in crop_ids:
            raise ValueError(f"Unknown rule candidate: {target.candidate}")
        if target.candidate_family and target.candidate_family not in families:
            raise ValueError(f"Unknown rule candidate family: {target.candidate_family}")
        if rule.condition.predecessor and rule.condition.predecessor not in crop_ids:
            raise ValueError(f"Unknown predecessor: {rule.condition.predecessor}")
    for pair in oracle:
        if pair.predecessor not in crop_ids or pair.candidate not in crop_ids or pair.region not in manifest.regions:
            raise ValueError(f"Unknown crop or region in oracle: {pair.id}")
    paths = sorted([root / "manifest.yaml", root / "catalog.yaml", root / "multipliers.yaml", root / "oracle.csv",
                    *list((root / "sowing_windows").glob("*.csv")),
                    *list((root / "rotation").rglob("*.yaml"))])
    digest = hashlib.sha256()
    for path in paths:
        content = path.read_bytes()
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(len(content).to_bytes(8, "big") + content)
    content_hash = digest.hexdigest()
    return RulesBundle(version=f"{manifest.version}+{content_hash[:12]}", content_hash=content_hash,
                       manifest=manifest, catalog=catalog, sowing_windows=windows,
                       rotation=rotation, multipliers=multipliers, oracle=oracle)


def load_rules(root: str | Path = "rules", *, allow_demo: bool = False) -> domain.RuleSet:
    """Return runtime contracts after review validation, keeping draft status intact."""
    bundle = load_rule_bundle(root, allow_demo=allow_demo)
    catalog = []
    for crop in bundle.catalog:
        data = crop.model_dump()
        data["regions"] = data.pop("supported_regions")
        # Domain uses exact for unsupported crops; a null code remains unsupported.
        data["gaez_match"] = data["gaez_match"] or "exact"
        catalog.append(domain.CropVariant.model_validate(data))
    windows = []
    for window in bundle.sowing_windows:
        data = window.model_dump(exclude={"optimal_from", "optimal_to", "version"})
        data["prep_days"] = data.pop("min_prep_days")
        windows.append(domain.SowingWindow.model_validate(data))
    rotation = []
    for rule in bundle.rotation:
        data = rule.model_dump()
        data["condition"]["min_days"] = data["condition"].pop("min_days_between")
        rotation.append(domain.RotationRule.model_validate(data))
    statuses = {parameter.status for parameter in bundle.multipliers}
    status = "approved" if statuses == {"approved"} else "demo" if statuses == {"demo"} else "draft"
    # A partially reviewed numerical configuration stays disabled as a whole.
    params = {"status": status}
    if status in {"approved", "demo"}:
        sources = [parameter.source for parameter in bundle.multipliers]
        params["source"] = {
            "type": "reviewed_bundle" if status == "approved" else "synthetic_demo",
            "ref": bundle.version,
            "verified_by": ", ".join(sorted({source.verified_by for source in sources if source.verified_by})) or None,
            "verified_at": max((source.verified_at for source in sources if source.verified_at), default=None),
        }
        params["effect_multipliers"] = {}
        params["weather_multipliers"] = {}
        for parameter in bundle.multipliers:
            name = parameter.id.split(".", 1)[-1]
            interval = {key: getattr(parameter, key) for key in ("lo", "mid", "hi")} if parameter.lo is not None else None
            if parameter.category == "rotation":
                if name == "missing":
                    params["rotation_missing"] = interval
                else:
                    params["effect_multipliers"][name] = parameter.value
            elif parameter.category == "weather":
                if name == "missing":
                    params["weather_missing"] = interval
                elif interval is not None:
                    params["weather_multipliers"][name] = interval
            else:
                params[name] = parameter.value
    return domain.RuleSet(version=bundle.version, catalog=catalog, sowing_windows=windows,
                          rotation=rotation, parameters=domain.RankingParameters.model_validate(params),
                          demo=bundle.manifest.status == "demo" or any(
                              record.status == "demo" for record in
                              [*bundle.catalog, *bundle.sowing_windows, *bundle.rotation, *bundle.multipliers, *bundle.oracle]))
