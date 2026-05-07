"""Tests für forge_execute.scoring."""

from __future__ import annotations

import pytest
from forge_core.spec import ProjectSpec
from forge_execute.scoring import ScoreBaseline, compute_composite, keep_or_discard


def _spec_with_scores(scores: list[dict]) -> ProjectSpec:
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
            "scores": scores,
        }
    )


def test_no_scores_returns_none() -> None:
    spec = _spec_with_scores([])
    assert compute_composite(measurements={}, spec=spec) is None


def test_no_measurements_returns_none() -> None:
    spec = _spec_with_scores([{"kind": "coverage_pct", "weight": 1.0}])
    assert compute_composite(measurements={}, spec=spec) is None


def test_single_higher_is_better_score_with_heuristic() -> None:
    spec = _spec_with_scores([{"kind": "coverage_pct", "weight": 1.0}])
    # heuristic scale = 100, actual = 87.2, normalized = 0.872
    composite = compute_composite(measurements={"coverage_pct": 87.2}, spec=spec)
    assert composite is not None
    assert composite == pytest.approx(0.872)


def test_single_lower_is_better_score_with_heuristic() -> None:
    spec = _spec_with_scores(
        [{"kind": "ruff_warnings", "weight": 1.0, "lower_is_better": True}]
    )
    # 0 ruff warnings => normalized = 1 - 0/200 = 1.0
    composite = compute_composite(measurements={"ruff_warnings": 0}, spec=spec)
    assert composite == pytest.approx(1.0)


def test_lower_is_better_clamped_to_zero() -> None:
    spec = _spec_with_scores(
        [{"kind": "ruff_warnings", "weight": 1.0, "lower_is_better": True}]
    )
    # 1000 warnings ist sehr schlecht, soll bei 0 enden
    composite = compute_composite(measurements={"ruff_warnings": 1000}, spec=spec)
    assert composite == pytest.approx(0.0)


def test_weighted_sum_two_scores() -> None:
    spec = _spec_with_scores(
        [
            {"kind": "coverage_pct", "weight": 0.5},
            {"kind": "ruff_warnings", "weight": 0.5, "lower_is_better": True},
        ]
    )
    # coverage 80 → 0.8, ruff 0 → 1.0; weighted: 0.5*0.8 + 0.5*1.0 = 0.9
    composite = compute_composite(
        measurements={"coverage_pct": 80, "ruff_warnings": 0}, spec=spec
    )
    assert composite == pytest.approx(0.9)


def test_partial_measurements_renormalize_weights() -> None:
    """Wenn ein Score fehlt, fällt sein Gewicht weg, aber das Ergebnis bleibt in [0,1]."""
    spec = _spec_with_scores(
        [
            {"kind": "coverage_pct", "weight": 0.7},
            {"kind": "p95_latency_ms", "weight": 0.3, "lower_is_better": True},
        ]
    )
    composite = compute_composite(measurements={"coverage_pct": 80}, spec=spec)
    # nur coverage: 0.8 / 0.7 weight, /0.7 used weight = 0.8
    assert composite == pytest.approx(0.8)


def test_baseline_used_when_present() -> None:
    spec = _spec_with_scores([{"kind": "coverage_pct", "weight": 1.0}])
    # Mit baseline=80, scale=160; actual=80 → 0.5
    composite = compute_composite(
        measurements={"coverage_pct": 80},
        spec=spec,
        baseline=ScoreBaseline({"coverage_pct": 80}),
    )
    assert composite == pytest.approx(0.5)


def test_keep_or_discard_improvement() -> None:
    keep, reason = keep_or_discard(new_composite=0.8, baseline_composite=0.7)
    assert keep is True
    assert reason == "improvement"


def test_keep_or_discard_within_tolerance() -> None:
    keep, reason = keep_or_discard(
        new_composite=0.715, baseline_composite=0.7, tolerance=0.02
    )
    assert keep is False
    assert reason == "no_significant_change"


def test_keep_or_discard_regression() -> None:
    keep, reason = keep_or_discard(
        new_composite=0.6, baseline_composite=0.7, tolerance=0.02
    )
    assert keep is False
    assert reason == "regression"


def test_keep_or_discard_first_generation_with_positive_score() -> None:
    keep, reason = keep_or_discard(new_composite=0.5, baseline_composite=None)
    assert keep is True
    assert reason == "improvement"


def test_keep_or_discard_no_score() -> None:
    keep, reason = keep_or_discard(new_composite=None, baseline_composite=None)
    assert keep is False
    assert reason == "no_significant_change"


# --- Spec v0.3 Teil 5.2: drei explizite Modi ----------------------------


def test_modus1_gate_revival_with_explicit_maps() -> None:
    """Vorher-rotes Gate jetzt grün, vorher-grüne Gates bleiben grün
    → KEEP, reason `gate_revival`."""
    keep, reason = keep_or_discard(
        new_composite=0.5,
        baseline_composite=0.5,
        baseline_gate_results={"pytest_pass_rate": False, "ruff_warnings": True},
        new_gate_results={"pytest_pass_rate": True, "ruff_warnings": True},
    )
    assert keep is True
    assert reason == "gate_revival"


def test_modus2_composite_optimization_pure() -> None:
    """Alle Gates waren grün und sind grün, Composite besser → KEEP `improvement`."""
    keep, reason = keep_or_discard(
        new_composite=0.8,
        baseline_composite=0.7,
        baseline_gate_results={"pytest_pass_rate": True},
        new_gate_results={"pytest_pass_rate": True},
    )
    assert keep is True
    assert reason == "improvement"


def test_modus3_regression_pre_green_gate_now_red() -> None:
    """Vorher-grüne Gate ist jetzt rot — DISCARD, reason `regression`,
    egal was Composite sagt."""
    keep, reason = keep_or_discard(
        new_composite=0.99,  # toller Composite, irrelevant
        baseline_composite=0.5,
        baseline_gate_results={"pytest_pass_rate": True, "ruff_warnings": True},
        new_gate_results={"pytest_pass_rate": True, "ruff_warnings": False},
    )
    assert keep is False
    assert reason == "regression"


def test_strict_preservation_blocks_revival_if_other_gate_breaks() -> None:
    """Auch wenn ein Gate revived wird, blockt eine andere broken-gate die
    KEEP-Entscheidung (Modus 3 hat Vorrang)."""
    keep, reason = keep_or_discard(
        new_composite=0.5,
        baseline_composite=0.5,
        baseline_gate_results={"pytest_pass_rate": False, "ruff_warnings": True},
        new_gate_results={"pytest_pass_rate": True, "ruff_warnings": False},
    )
    # ruff war grün, ist jetzt rot → regression trumpft Revival
    assert keep is False
    assert reason == "regression"


def test_revival_with_multiple_red_gates_partial() -> None:
    """Genau ein vorher-rotes Gate wird grün, andere bleiben rot —
    KEEP (Modus 1), weil mind. eine vorher-rote jetzt grün UND
    keine vorher-grüne ist rot geworden."""
    keep, reason = keep_or_discard(
        new_composite=None,
        baseline_composite=None,
        baseline_gate_results={
            "pytest_pass_rate": False,
            "ruff_warnings": False,
            "mypy_errors": True,
        },
        new_gate_results={
            "pytest_pass_rate": True,    # revived
            "ruff_warnings": False,      # bleibt rot, OK
            "mypy_errors": True,         # bleibt grün, OK
        },
    )
    assert keep is True
    assert reason == "gate_revival"


def test_no_change_in_gates_falls_through_to_composite() -> None:
    """Gate-Map identisch (alle grün) → Composite-Vergleich entscheidet."""
    keep, reason = keep_or_discard(
        new_composite=0.71,  # innerhalb tolerance von 0.7
        baseline_composite=0.7,
        baseline_gate_results={"pytest_pass_rate": True},
        new_gate_results={"pytest_pass_rate": True},
    )
    assert keep is False
    assert reason == "no_significant_change"
