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
    # Surfaces aus der echten PINTA-Spec
    assert "backend_services" in spec.surfaces
    assert "backend_agent_tools" in spec.surfaces
    assert "agent_prompts" in spec.surfaces
    assert "frontend_components" in spec.surfaces
    # Forbidden enthält auth/payments/agent
    assert "backend/src/routes/auth.py" in spec.forbidden
    assert "backend/src/routes/agent.py" in spec.forbidden
    # Conservative cost cap für Smoke-Phase
    assert spec.cost_caps.per_run_usd == Decimal("1.50")


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


# --- Spec v0.3: Trigger-spezifische agents-Listen --------------------------


def test_trigger_default_agents_for_issue_label() -> None:
    d = _minimal_spec_dict()
    d["triggers"] = {
        "on_issue_label": {
            "auto-fix": {"strategy": "sequential", "model": "sonnet", "max_iterations": 3},
        },
    }
    spec = ProjectSpec.model_validate(d)
    auto_fix = spec.triggers.on_issue_label["auto-fix"]
    assert auto_fix.agents == ["architect", "developer", "tester"]


def test_trigger_default_agents_for_ci_failure_is_developer_only() -> None:
    d = _minimal_spec_dict()
    d["triggers"] = {
        "on_ci_failure": {"strategy": "sequential", "model": "sonnet"},
    }
    spec = ProjectSpec.model_validate(d)
    assert spec.triggers.on_ci_failure.agents == ["developer"]


def test_trigger_default_agents_for_pr_opened_is_reviewer_only() -> None:
    d = _minimal_spec_dict()
    d["triggers"] = {
        "on_pr_opened": {"strategy": "review_only", "model": "sonnet"},
    }
    spec = ProjectSpec.model_validate(d)
    assert spec.triggers.on_pr_opened.agents == ["reviewer"]


def test_trigger_explicit_agents_override_default() -> None:
    d = _minimal_spec_dict()
    d["triggers"] = {
        "on_issue_label": {
            "auto-feature": {
                "strategy": "sequential",
                "model": "opus",
                "max_iterations": 10,
                "agents": ["architect", "developer", "tester", "reviewer"],
            },
        },
    }
    spec = ProjectSpec.model_validate(d)
    assert spec.triggers.on_issue_label["auto-feature"].agents == [
        "architect", "developer", "tester", "reviewer",
    ]


def test_invalid_agent_name_rejected() -> None:
    d = _minimal_spec_dict()
    d["triggers"] = {
        "on_issue_label": {
            "auto-fix": {
                "strategy": "sequential",
                "model": "sonnet",
                "agents": ["architect", "magicworker"],  # magicworker existiert nicht
            },
        },
    }
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


# --- BoardConfig (v0.4) ------------------------------------------------


def test_board_config_optional_default_none() -> None:
    """Spec ohne board: bleibt None — kein Default ohne Operator-opt-in."""
    spec = ProjectSpec.model_validate(_minimal_spec_dict())
    assert spec.board is None


def test_board_config_minimal_valid() -> None:
    d = _minimal_spec_dict()
    d["board"] = {"owner": "rudi77", "project_number": 3}
    spec = ProjectSpec.model_validate(d)
    assert spec.board is not None
    assert spec.board.provider == "github"
    assert spec.board.owner == "rudi77"
    assert spec.board.project_number == 3
    assert spec.board.filter_status == "Todo"
    assert spec.board.filter_labels == ["bug"]
    assert spec.board.default_focus_template == "issue-{number}"
    assert spec.board.default_template_id == "board_loop_v1"


def test_board_config_full_overrides() -> None:
    d = _minimal_spec_dict()
    d["board"] = {
        "owner": "acme",
        "project_number": 7,
        "filter_status": "In Progress",
        "filter_labels": ["bug", "ready"],
        "default_focus_template": "fix-{number}",
        "default_template_id": "auto_v2",
    }
    spec = ProjectSpec.model_validate(d)
    assert spec.board is not None
    assert spec.board.filter_status == "In Progress"
    assert spec.board.filter_labels == ["bug", "ready"]
    assert spec.board.default_focus_template == "fix-{number}"
    assert spec.board.default_template_id == "auto_v2"


def test_board_config_project_number_must_be_positive() -> None:
    d = _minimal_spec_dict()
    d["board"] = {"owner": "rudi77", "project_number": 0}
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_board_config_owner_must_be_non_empty() -> None:
    d = _minimal_spec_dict()
    d["board"] = {"owner": "", "project_number": 3}
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_board_config_unknown_provider_rejected() -> None:
    d = _minimal_spec_dict()
    d["board"] = {"provider": "gitlab", "owner": "rudi77", "project_number": 3}
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_board_config_extra_field_rejected() -> None:
    d = _minimal_spec_dict()
    d["board"] = {
        "owner": "rudi77",
        "project_number": 3,
        "magic_field": "boom",  # extra="forbid"
    }
    with pytest.raises((ValueError, SpecValidationError)):
        ProjectSpec.model_validate(d)


def test_board_config_empty_filter_labels_allowed() -> None:
    """Leere Label-Liste = AND-trivial = alle Labels akzeptiert. Bewusst erlaubt."""
    d = _minimal_spec_dict()
    d["board"] = {"owner": "rudi77", "project_number": 3, "filter_labels": []}
    spec = ProjectSpec.model_validate(d)
    assert spec.board is not None
    assert spec.board.filter_labels == []
