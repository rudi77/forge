"""Tests für forge_core.spec."""

from __future__ import annotations

import warnings
from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.spec import (
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    SpecValidationError,
    load_spec,
)


def _minimal_spec_dict() -> dict:
    return {
        "spec_version": "1.0",
        "name": "test",
        "cost_caps": {
            "per_generation_usd": "0.5",
            "per_run_usd": "5",
            "per_project_per_day_usd": "30",
            "per_project_per_month_usd": "500",
        },
    }


def test_minimal_spec_validates() -> None:
    spec = ProjectSpec.model_validate(_minimal_spec_dict())
    assert spec.name == "test"
    assert spec.capabilities.merge_pr is False
    assert spec.capabilities.push_to_main is False


def test_pinta_reference_spec_loads(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[3]
    pinta = repo_root / "examples" / "pinta" / ".forge" / "project.yaml"
    if not pinta.exists():
        pytest.skip(f"reference spec not found at {pinta}")
    spec = load_spec(pinta)
    assert spec.name == "pinta"
    assert "backend_logic" in spec.surfaces
    assert spec.cost_caps.per_run_usd == Decimal("5.00")


def test_merge_pr_true_rejected() -> None:
    with pytest.raises((ValueError, SpecValidationError)):
        CapabilitiesConfig(merge_pr=True)  # type: ignore[arg-type]


def test_push_to_main_true_rejected() -> None:
    with pytest.raises((ValueError, SpecValidationError)):
        CapabilitiesConfig(push_to_main=True)  # type: ignore[arg-type]


def test_push_force_true_rejected() -> None:
    with pytest.raises((ValueError, SpecValidationError)):
        CapabilitiesConfig(push_force=True)  # type: ignore[arg-type]


def test_overlapping_surface_paths_rejected() -> None:
    d = _minimal_spec_dict()
    d["surfaces"] = {
        "a": {"paths": ["src/foo/"], "type": "code"},
        "b": {"paths": ["src/foo/"], "type": "code"},
    }
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_forbidden_path_in_surface_rejected() -> None:
    d = _minimal_spec_dict()
    d["surfaces"] = {"a": {"paths": ["src/foo/"], "type": "code"}}
    d["forbidden"] = ["src/foo/"]
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_yaml_keys_surface_requires_allowed_keys() -> None:
    d = _minimal_spec_dict()
    d["surfaces"] = {"a": {"paths": ["agents/foo.yaml"], "type": "yaml-keys"}}
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_score_weights_normalized_with_warning() -> None:
    d = _minimal_spec_dict()
    d["scores"] = [
        {"kind": "coverage_pct", "weight": 0.7},
        {"kind": "todo_count", "weight": 0.5, "lower_is_better": True},
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        spec = ProjectSpec.model_validate(d)
    assert len(caught) == 1
    assert "normalizing" in str(caught[0].message)
    weights = [s.weight for s in spec.scores]
    assert sum(weights) == pytest.approx(1.0)


def test_dangling_eval_suite_reference_rejected() -> None:
    d = _minimal_spec_dict()
    d["eval_suites"] = {"quick": {"cmd": "pytest", "budget_s": 60}}
    d["gates"] = [{"kind": "pytest_pass_rate", "threshold": 1.0, "source": "nonexistent"}]
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_eval_suites_must_include_quick() -> None:
    d = _minimal_spec_dict()
    d["eval_suites"] = {"full": {"cmd": "scripts/run_full.sh", "budget_s": 600}}
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_cost_caps_must_be_monotone() -> None:
    with pytest.raises((ValueError, SpecValidationError)):
        CostCapsConfig(
            per_generation_usd=Decimal("10"),
            per_run_usd=Decimal("5"),
            per_project_per_day_usd=Decimal("30"),
            per_project_per_month_usd=Decimal("500"),
        )


def test_gate_must_have_threshold_or_max_increase() -> None:
    d = _minimal_spec_dict()
    d["gates"] = [{"kind": "pytest_pass_rate"}]
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_surface_for_path_lookup() -> None:
    d = _minimal_spec_dict()
    d["surfaces"] = {"backend": {"paths": ["backend/src/services/"], "type": "code"}}
    spec = ProjectSpec.model_validate(d)
    assert spec.surface_for_path("backend/src/services/calc.py") == ("backend", spec.surfaces["backend"])
    assert spec.surface_for_path("frontend/foo.ts") is None


def test_dump_then_load_roundtrip(tmp_path: Path) -> None:
    from forge_core.spec import dump_spec
    spec = ProjectSpec.model_validate(_minimal_spec_dict())
    out = tmp_path / "project.yaml"
    dump_spec(spec, out)
    reloaded = load_spec(out)
    assert reloaded.name == spec.name
    assert reloaded.cost_caps == spec.cost_caps
