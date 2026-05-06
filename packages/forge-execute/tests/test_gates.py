"""Tests für forge_execute.gates."""

from __future__ import annotations

from decimal import Decimal

import pytest

from forge_core.spec import GateConfig, ProjectSpec
from forge_execute.gates import GateBaseline, evaluate_gates


def _spec_with_gates(gates: list[dict]) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "test",
            "cost_caps": {
                "per_generation_usd": "0.5",
                "per_run_usd": "5",
                "per_project_per_day_usd": "30",
                "per_project_per_month_usd": "500",
            },
            "gates": gates,
        }
    )


def test_threshold_higher_is_better_passes() -> None:
    spec = _spec_with_gates([{"kind": "coverage_pct", "threshold": 80.0}])
    passed, results = evaluate_gates(measurements={"coverage_pct": 87.2}, spec=spec)
    assert passed is True
    assert results[0].passed is True
    assert results[0].actual == 87.2


def test_threshold_higher_is_better_fails() -> None:
    spec = _spec_with_gates([{"kind": "coverage_pct", "threshold": 90.0}])
    passed, results = evaluate_gates(measurements={"coverage_pct": 87.2}, spec=spec)
    assert passed is False
    assert results[0].passed is False
    assert "actual" in (results[0].reason or "")


def test_threshold_lower_is_better_passes() -> None:
    spec = _spec_with_gates(
        [{"kind": "ruff_warnings", "threshold": 0, "lower_is_better": True}]
    )
    passed, _ = evaluate_gates(measurements={"ruff_warnings": 0}, spec=spec)
    assert passed is True


def test_threshold_lower_is_better_fails_on_excess() -> None:
    spec = _spec_with_gates(
        [{"kind": "ruff_warnings", "threshold": 0, "lower_is_better": True}]
    )
    passed, _ = evaluate_gates(measurements={"ruff_warnings": 3}, spec=spec)
    assert passed is False


def test_max_increase_no_baseline_accepts() -> None:
    """Erste Generation hat keine Baseline — als Pass durchwinken."""
    spec = _spec_with_gates([{"kind": "mypy_errors", "max_increase": 0}])
    passed, results = evaluate_gates(
        measurements={"mypy_errors": 17}, spec=spec, baseline=GateBaseline({})
    )
    assert passed is True
    assert "no baseline" in (results[0].reason or "").lower()


def test_max_increase_with_baseline_blocks_growth() -> None:
    spec = _spec_with_gates([{"kind": "mypy_errors", "max_increase": 0}])
    passed, results = evaluate_gates(
        measurements={"mypy_errors": 18},
        spec=spec,
        baseline=GateBaseline({"mypy_errors": 17}),
    )
    assert passed is False
    assert "increased" in (results[0].reason or "")


def test_max_increase_allows_decrease() -> None:
    spec = _spec_with_gates([{"kind": "mypy_errors", "max_increase": 0}])
    passed, _ = evaluate_gates(
        measurements={"mypy_errors": 16},
        spec=spec,
        baseline=GateBaseline({"mypy_errors": 17}),
    )
    assert passed is True


def test_missing_measurement_fails_gate() -> None:
    spec = _spec_with_gates([{"kind": "coverage_pct", "threshold": 80.0}])
    passed, results = evaluate_gates(measurements={}, spec=spec)
    assert passed is False
    assert "no measurement" in (results[0].reason or "")


def test_all_gates_must_pass() -> None:
    spec = _spec_with_gates(
        [
            {"kind": "coverage_pct", "threshold": 80.0},
            {"kind": "ruff_warnings", "threshold": 0, "lower_is_better": True},
        ]
    )
    passed, _ = evaluate_gates(
        measurements={"coverage_pct": 87.2, "ruff_warnings": 5}, spec=spec
    )
    assert passed is False  # ruff_warnings ist über threshold


def test_no_gates_passes_trivially() -> None:
    spec = _spec_with_gates([])
    passed, results = evaluate_gates(measurements={}, spec=spec)
    assert passed is True
    assert results == []
