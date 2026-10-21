from pathlib import Path
import shutil

import pytest
import yaml
from pydantic import ValidationError

from app.domain import RuleSet
from app.rules import load_rule_bundle, load_rules
from app.rules.models import Multiplier, OraclePair, RotationRule, SowingWindow


ROOT = Path(__file__).resolve().parents[1] / "rules"
SIGNED = {"type": "test_review", "ref": "Test-only reference", "verified_by": "Test-only reviewer", "verified_at": "2026-01-01"}


def copied(tmp_path, *, demo=False):
    target = tmp_path / "rules"
    shutil.copytree(ROOT / "demo" if demo else ROOT, target)
    return target


def test_unreviewed_real_bundle_cannot_activate_rules_or_numbers():
    bundle = load_rule_bundle(ROOT)
    runtime = load_rules(ROOT)
    assert isinstance(runtime, RuleSet)
    assert 8 <= len(bundle.catalog) <= 12
    assert 30 <= len(bundle.oracle) <= 50
    assert all(pair.expected_effect is None for pair in bundle.oracle)
    assert not bundle.active_catalog
    assert not bundle.active_windows
    assert not bundle.active_rotation
    assert not bundle.active_multipliers
    assert runtime.parameters.status == "draft"
    assert runtime.parameters.floor_agro is None
    assert not runtime.parameters.effect_multipliers
    assert all(window.earliest is None and window.latest is None for window in runtime.sowing_windows)
    assert bundle.readiness_issues("UA-53")


def test_demo_requires_explicit_opt_in_and_never_claims_approval():
    with pytest.raises(ValueError, match="Demo"):
        load_rules(ROOT / "demo")
    runtime = load_rules(ROOT / "demo", allow_demo=True)
    assert runtime.demo
    assert runtime.parameters.status == "demo"
    assert runtime.parameters.floor_agro == 0.4
    assert all(crop.status == "demo" for crop in runtime.catalog)
    assert runtime.parameters.source.verified_by is None
    assert not load_rule_bundle(ROOT / "demo", allow_demo=True).active_multipliers


def test_one_demo_record_is_rejected_even_in_draft_bundle(tmp_path):
    root = copied(tmp_path)
    path = root / "catalog.yaml"
    data = yaml.safe_load(path.read_text())
    data[0]["status"] = "demo"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Demo"):
        load_rules(root)


@pytest.mark.parametrize("field", ["verified_by", "verified_at", "ref"])
def test_approved_parameters_require_real_review_metadata(field):
    source = dict(SIGNED)
    source[field] = None
    with pytest.raises(ValidationError):
        Multiplier.model_validate({"id": "rotation.neutral", "category": "rotation", "status": "approved", "value": 1, "source": source})


def test_approved_calendar_cannot_keep_unknown_dates():
    with pytest.raises(ValidationError, match="all dates"):
        SowingWindow.model_validate({"region": "UA-53", "crop_variant": "wheat_winter", "version": 1, "status": "approved", "source": SIGNED})


def test_approved_rule_cannot_keep_unknown_return_interval():
    data = yaml.safe_load((ROOT / "rotation/ua/review.yaml").read_text())[0]
    data.update(status="approved", source=SIGNED)
    with pytest.raises(ValidationError, match="min_years_between"):
        RotationRule.model_validate(data)


def test_oracle_approval_requires_human_verdict():
    with pytest.raises(ValidationError, match="verdict"):
        OraclePair.model_validate({"id": "test", "region": "UA-53", "predecessor": "soybean", "candidate": "maize_grain", "status": "approved", "source": SIGNED})


def test_versions_are_reproducible_and_sensitive_to_review_changes(tmp_path):
    root = copied(tmp_path)
    first = load_rules(root).version
    assert first == load_rules(root).version == load_rules(ROOT).version
    path = root / "rotation/ua/review.yaml"
    path.write_text(path.read_text() + "\n# Human review note\n")
    assert load_rules(root).version != first


def test_nested_demo_edits_do_not_change_production_rule_version(tmp_path):
    root = copied(tmp_path)
    first = load_rules(root).version
    path = root / "demo/catalog.yaml"
    path.write_text(path.read_text() + "\n# fixture edit\n")
    assert load_rules(root).version == first


def test_catalog_cross_references_are_checked(tmp_path):
    root = copied(tmp_path)
    path = root / "rotation/ua/review.yaml"
    data = yaml.safe_load(path.read_text())
    data[0]["applies_to"]["candidate"] = "misspelled_crop"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ValueError, match="Unknown rule candidate"):
        load_rules(root)


def test_partial_numeric_approval_stays_disabled(tmp_path):
    root = copied(tmp_path)
    path = root / "multipliers.yaml"
    data = yaml.safe_load(path.read_text())
    data[0].update(status="approved", source=SIGNED, value=1.05)
    path.write_text(yaml.safe_dump(data))
    runtime = load_rules(root)
    assert runtime.parameters.status == "draft"
    assert not runtime.parameters.effect_multipliers


@pytest.mark.parametrize("patch", [{"earliest": "09-31"}, {"earliest": "9-15"}, {"optimal_from": "08-01"}])
def test_invalid_or_inconsistent_calendar_rejected(patch):
    data = {"region": "UA-53", "crop_variant": "wheat_winter", "version": 1, "status": "approved", "source": SIGNED,
            "earliest": "09-15", "latest": "10-20", "optimal_from": "09-25", "optimal_to": "10-10", "min_prep_days": 7, "min_lead_days": 0}
    data.update(patch)
    with pytest.raises(ValidationError):
        SowingWindow.model_validate(data)
