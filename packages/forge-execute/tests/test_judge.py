"""Tests für die Judge-Phase (Spec v0.5).

Drei Ebenen:
1. ``JudgeEvaluator`` — fail-closed-Verhalten gegen den ``CodingAgent``.
2. ``MockCodingAgent.review`` — die drei Mock-Modi.
3. End-to-End im ``SequentialRunner`` — ein Feature-Diff wird über ein
   ``llm_judge_score``-Gate als rot→grün-Revival behalten; ein
   schlechtes Judge-Urteil führt zu DISCARD.
"""

from __future__ import annotations

import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.blobs import BlobStore
from forge_core.events import EventKind
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from forge_execute.agents import MockCodingAgent, ReviewResult
from forge_execute.agents.base import CodingAgentError, CodingAgentTimeout
from forge_execute.evaluators.judge import JUDGE_MEASUREMENT_KEY, JudgeEvaluator
from forge_execute.runner import RunConfig, SequentialRunner

# --- JudgeEvaluator: fail-closed ---------------------------------------


class _RaisingAgent:
    """Minimaler Agent, dessen review() eine vorgegebene Exception wirft."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def propose(self, **kwargs):  # pragma: no cover - im Judge-Test ungenutzt
        raise NotImplementedError

    def review(self, **kwargs) -> ReviewResult:
        raise self._exc


def test_judge_evaluator_passes_through_review() -> None:
    agent = MockCodingAgent(
        callable_=lambda wt, p: None,
        review_result=ReviewResult(
            judge_score=0.9, verdict="pass", reasoning="gut", cost_usd=Decimal("0.01")
        ),
    )
    judge = JudgeEvaluator(agent)
    outcome = judge.run(
        worktree=Path("."),
        acceptance_criteria="tu X",
        diff="diff",
        max_turns=4,
        budget_usd=Decimal("0.5"),
    )
    assert outcome.score == 0.9
    assert outcome.verdict == "pass"
    assert outcome.error is None
    assert outcome.measurement == {JUDGE_MEASUREMENT_KEY: 0.9}


def test_judge_evaluator_fail_closed_on_error() -> None:
    judge = JudgeEvaluator(_RaisingAgent(CodingAgentError("claude crashed")))
    outcome = judge.run(
        worktree=Path("."),
        acceptance_criteria="x",
        diff="d",
        max_turns=4,
        budget_usd=Decimal("0.5"),
    )
    assert outcome.score == 0.0
    assert outcome.verdict == "fail"
    assert outcome.error is not None
    assert "claude crashed" in outcome.reasoning


def test_judge_evaluator_fail_closed_on_timeout() -> None:
    judge = JudgeEvaluator(_RaisingAgent(CodingAgentTimeout("too slow")))
    outcome = judge.run(
        worktree=Path("."),
        acceptance_criteria="x",
        diff="d",
        max_turns=4,
        budget_usd=Decimal("0.5"),
    )
    assert outcome.score == 0.0
    assert outcome.verdict == "fail"


# --- MockCodingAgent.review modes --------------------------------------


def test_mock_review_default_is_pass() -> None:
    agent = MockCodingAgent(callable_=lambda wt, p: None)
    out = agent.review(
        worktree=Path("."),
        acceptance_criteria="x",
        diff="d",
        max_turns=1,
        budget_usd=Decimal("0"),
    )
    assert out.verdict == "pass"
    assert out.judge_score == 1.0
    assert agent.review_calls == [(Path("."), "x", "d")]


def test_mock_review_sequence() -> None:
    agent = MockCodingAgent(
        callable_=lambda wt, p: None,
        review_sequence=[
            ReviewResult(judge_score=0.2, verdict="fail"),
            ReviewResult(judge_score=0.95, verdict="pass"),
        ],
    )
    first = agent.review(
        worktree=Path("."), acceptance_criteria="x", diff="d",
        max_turns=1, budget_usd=Decimal("0"),
    )
    second = agent.review(
        worktree=Path("."), acceptance_criteria="x", diff="d",
        max_turns=1, budget_usd=Decimal("0"),
    )
    assert first.verdict == "fail"
    assert second.verdict == "pass"


# --- End-to-End im Runner ----------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def feature_repo(tmp_path: Path) -> Path:
    """Repo mit grünem Test-Baseline — ein Feature soll hinzugefügt werden."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@forge.local")
    _git(repo, "config", "user.name", "forge-test")
    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "lib.py").write_text(
        "def greet(name):\n    return f'hi {name}'\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_lib.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
        "from src.lib import greet\n\n"
        "def test_greet():\n    assert greet('x') == 'hi x'\n",
        encoding="utf-8",
    )
    # Realistisch wie jedes Python-Repo: pytest-Artefakte ignorieren, damit der
    # Eval-Lauf den Worktree nicht mit untracked Junk verschmutzt.
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.coverage\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial (green)")
    return repo


def _judge_spec() -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "feature-repo",
            "cost_caps": {
                "per_generation_usd": "0.50",
                "per_run_usd": "5.00",
                "per_project_per_day_usd": "30.00",
                "per_project_per_month_usd": "500.00",
            },
            "surfaces": {"code": {"paths": ["src/"], "type": "code"}},
            "forbidden": [],
            "capabilities": {"run": ["pytest *", "python *"]},
            "eval_suites": {
                "quick": {
                    "cmd": f'"{sys.executable}" -m pytest -q --no-header --tb=no',
                    "budget_s": 60,
                    "parses": "pytest_json",
                },
            },
            "gates": [
                {"kind": "pytest_pass_rate", "threshold": 1.0, "source": "quick"},
                {"kind": "llm_judge_score", "threshold": 0.8},
            ],
            "scores": [{"kind": "test_count", "weight": 1.0}],
            "judge": {"enabled": True, "threshold": 0.8},
        }
    )


def _add_feature(wt: Path, prompt: str) -> None:
    """Agent implementiert ein neues Feature (additive Funktion)."""
    target = wt / "src" / "lib.py"
    target.write_text(
        "def greet(name):\n    return f'hi {name}'\n\n"
        "def shout(name):\n    return f'HI {name.upper()}'\n",
        encoding="utf-8",
    )
    return None


def test_runner_keeps_feature_when_judge_passes(
    feature_repo: Path, tmp_path: Path
) -> None:
    """Judge bestätigt das Feature → llm_judge_score-Gate rot→grün →
    gate_revival → KEEP, obwohl kein numerischer Score sich verbessert."""
    spec = _judge_spec()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    agent = MockCodingAgent(
        callable_=_add_feature,
        review_result=ReviewResult(
            judge_score=0.95, verdict="pass", reasoning="Feature erfüllt das Issue."
        ),
    )
    config = RunConfig(
        spec=spec,
        project="feature-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=feature_repo,
        prompt_template_id="feature_v1",
        initial_prompt="Füge eine shout()-Funktion hinzu.",
        acceptance_criteria="Es gibt eine shout(name)-Funktion, die schreit.",
        max_iterations=1,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    assert result.decision == "pr_created", f"got {result.decision}"
    assert any(g.kept for g in result.generations)

    events = store.events_for_run(result.run_id)
    kinds = [e.kind for e in events]
    # Judge-Eventpaar muss da sein (eval_mode="judge")
    judge_evals = [
        e for e in events
        if e.kind == EventKind.EVAL_FINISHED and e.payload["eval_mode"] == "judge"
    ]
    assert len(judge_evals) == 1
    assert judge_evals[0].payload["diagnostics"][JUDGE_MEASUREMENT_KEY] == 0.95
    assert EventKind.DECISION_MADE in kinds

    # Judge-Begründung als Blob-Artefakt
    reasoning_hash = judge_evals[0].artifacts.get("judge_reasoning")
    assert reasoning_hash and "Feature" in blobs.get_text(reasoning_hash)

    # Decision war gate_revival (Judge-Gate von rot auf grün)
    [decision] = [e for e in events if e.kind == EventKind.DECISION_MADE]
    assert decision.payload["kept"] is True
    assert decision.payload["reason"] == "gate_revival"
    store.close()


def test_runner_discards_feature_when_judge_fails(
    feature_repo: Path, tmp_path: Path
) -> None:
    """Schlechtes Judge-Urteil → llm_judge_score-Gate bleibt rot → DISCARD,
    auch wenn pytest grün ist."""
    spec = _judge_spec()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    agent = MockCodingAgent(
        callable_=_add_feature,
        review_result=ReviewResult(
            judge_score=0.2, verdict="fail", reasoning="Feature verfehlt das Issue."
        ),
    )
    config = RunConfig(
        spec=spec,
        project="feature-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=feature_repo,
        prompt_template_id="feature_v1",
        initial_prompt="Füge eine shout()-Funktion hinzu.",
        acceptance_criteria="Es gibt eine shout(name)-Funktion.",
        max_iterations=1,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    assert result.decision != "pr_created"
    assert not any(g.kept for g in result.generations)
    store.close()
