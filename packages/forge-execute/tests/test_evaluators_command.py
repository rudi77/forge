"""Tests für forge_execute.evaluators.command."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from forge_core.spec import EvalSuiteConfig
from forge_execute.evaluators import (
    CommandEvaluator,
    parse_pytest_output,
    parse_scores_json,
)

# --- Parser-Unit-Tests ---------------------------------------------------


def test_parse_pytest_all_passed() -> None:
    out = "============================= 5 passed in 0.23s =============================\n"
    measurements = parse_pytest_output(out, exit_code=0)
    assert measurements["pytest_pass_rate"] == pytest.approx(1.0)
    assert measurements["pytest_test_count"] == 5.0
    assert measurements["pytest_failed_count"] == 0.0


def test_parse_pytest_partial_failed() -> None:
    out = "========================= 2 failed, 3 passed in 0.45s =========================\n"
    measurements = parse_pytest_output(out, exit_code=1)
    assert measurements["pytest_pass_rate"] == pytest.approx(0.6)
    assert measurements["pytest_test_count"] == 5.0
    assert measurements["pytest_failed_count"] == 2.0


def test_parse_pytest_all_failed() -> None:
    out = "============================== 5 failed in 0.5s ==============================\n"
    measurements = parse_pytest_output(out, exit_code=1)
    assert measurements["pytest_pass_rate"] == pytest.approx(0.0)
    assert measurements["pytest_test_count"] == 5.0
    assert measurements["pytest_failed_count"] == 5.0


def test_parse_pytest_no_tests_ran() -> None:
    out = "no tests ran in 0.03s\n"
    measurements = parse_pytest_output(out, exit_code=5)
    assert measurements["pytest_pass_rate"] == 1.0
    assert measurements["pytest_test_count"] == 0.0


def test_parse_pytest_with_skipped_and_warnings() -> None:
    out = "===== 5 passed, 2 skipped, 3 warnings in 0.5s =====\n"
    measurements = parse_pytest_output(out, exit_code=0)
    assert measurements["pytest_pass_rate"] == pytest.approx(1.0)
    assert measurements["pytest_test_count"] == 5.0


def test_parse_pytest_falls_back_to_exit_code() -> None:
    measurements = parse_pytest_output("garbage output", exit_code=0)
    assert measurements["pytest_pass_rate"] == 1.0
    measurements_fail = parse_pytest_output("garbage output", exit_code=1)
    assert measurements_fail["pytest_pass_rate"] == 0.0


def test_parse_scores_json_simple() -> None:
    out = '{"coverage_pct": 87.2, "p95_latency_ms": 412.0}'
    m = parse_scores_json(out)
    assert m == {"coverage_pct": 87.2, "p95_latency_ms": 412.0}


def test_parse_scores_json_with_setup_logs_before() -> None:
    out = (
        "Running coverage...\n"
        "Done.\n"
        '{"coverage_pct": 85.5, "bundle_kb": 234.0}\n'
    )
    m = parse_scores_json(out)
    assert m == {"coverage_pct": 85.5, "bundle_kb": 234.0}


def test_parse_scores_json_ignores_non_numeric() -> None:
    out = '{"coverage_pct": 87.2, "name": "pinta", "ok": true}'
    m = parse_scores_json(out)
    assert m == {"coverage_pct": 87.2}


def test_parse_scores_json_empty_returns_empty() -> None:
    assert parse_scores_json("") == {}
    assert parse_scores_json("not json at all") == {}


# --- Integration: echte Subprozesse ----------------------------------------


def test_run_simple_pytest_via_python(tmp_path: Path) -> None:
    """Ruft einen echten pytest-Lauf gegen ein Mini-Test-Modul auf."""
    test_file = tmp_path / "test_mini.py"
    test_file.write_text(
        "def test_alpha():\n    assert 1 + 1 == 2\n\n"
        "def test_beta():\n    assert 'a' < 'b'\n",
        encoding="utf-8",
    )
    suite = EvalSuiteConfig(
        cmd=f'"{sys.executable}" -m pytest -q --no-header',
        budget_s=60,
        parses="pytest_json",
    )
    evaluator = CommandEvaluator(tool_probes=())  # leere Probes für Speed
    result = evaluator.run(suite_id="quick", suite=suite, cwd=tmp_path)
    assert result.success is True
    assert result.exit_code == 0
    assert result.measurements["pytest_pass_rate"] == pytest.approx(1.0)
    assert result.measurements["pytest_test_count"] == 2.0
    assert result.timeout is False


def test_run_pytest_with_failure(tmp_path: Path) -> None:
    test_file = tmp_path / "test_red.py"
    test_file.write_text(
        "def test_passing():\n    assert True\n\n"
        "def test_failing():\n    assert False\n",
        encoding="utf-8",
    )
    suite = EvalSuiteConfig(
        cmd=f'"{sys.executable}" -m pytest -q --no-header --tb=no',
        budget_s=60,
        parses="pytest_json",
    )
    evaluator = CommandEvaluator(tool_probes=())
    result = evaluator.run(suite_id="quick", suite=suite, cwd=tmp_path)
    assert result.success is False  # exit != 0
    assert result.measurements["pytest_pass_rate"] == pytest.approx(0.5)
    assert result.measurements["pytest_failed_count"] == 1.0


def test_run_timeout_kills_command(tmp_path: Path) -> None:
    """Lange laufender Command muss durch budget_s gestoppt werden."""
    suite = EvalSuiteConfig(
        cmd=f'"{sys.executable}" -c "import time; time.sleep(30)"',
        budget_s=1,
        parses="raw",
    )
    evaluator = CommandEvaluator(tool_probes=())
    result = evaluator.run(suite_id="slow", suite=suite, cwd=tmp_path)
    assert result.timeout is True
    assert result.success is False
    # Timeout darf nicht mehr als ~ein paar Sekunden über die budget gehen
    assert result.duration_ms < 10_000


def test_run_collects_tool_versions() -> None:
    """tool_versions enthält mindestens git und python (sind im CI immer da)."""
    suite = EvalSuiteConfig(cmd="echo ok", budget_s=10, parses="raw")
    evaluator = CommandEvaluator()
    result = evaluator.run(suite_id="x", suite=suite, cwd=Path.cwd())
    # python und git sollten gefunden werden
    assert "python" in result.tool_versions or "git" in result.tool_versions


def test_run_scores_json(tmp_path: Path) -> None:
    suite = EvalSuiteConfig(
        cmd=f'"{sys.executable}" -c "import json; print(json.dumps({{\'coverage_pct\': 92.5, \'bundle_kb\': 187.0}}))"',
        budget_s=10,
        parses="scores_json",
    )
    evaluator = CommandEvaluator(tool_probes=())
    result = evaluator.run(suite_id="full", suite=suite, cwd=tmp_path)
    assert result.success is True
    assert result.measurements == {"coverage_pct": 92.5, "bundle_kb": 187.0}


def test_run_raw_no_parsing(tmp_path: Path) -> None:
    suite = EvalSuiteConfig(cmd="echo hello", budget_s=5, parses="raw")
    evaluator = CommandEvaluator(tool_probes=())
    result = evaluator.run(suite_id="x", suite=suite, cwd=tmp_path)
    assert result.success is True
    assert result.measurements == {}
    assert "hello" in result.stdout
