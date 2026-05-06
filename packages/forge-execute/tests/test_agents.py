"""Tests für forge_execute.agents."""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from forge_execute.agents import (
    ClaudeCodeCLIAgent,
    CodingAgent,
    CodingAgentError,
    MockCodingAgent,
    ProposalResult,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@forge.local")
    _git(repo_root, "config", "user.name", "forge-test")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "initial")
    return repo_root


# --- MockCodingAgent ----------------------------------------------------


def test_mock_static_result_used_for_every_call() -> None:
    expected = ProposalResult(diff="--- a\n+++ b\n", tokens_in=42, tokens_out=10)
    agent = MockCodingAgent(static_result=expected)
    r1 = agent.propose(worktree=Path("/tmp"), prompt="x", max_turns=1, budget_usd=Decimal("1"))
    r2 = agent.propose(worktree=Path("/tmp"), prompt="y", max_turns=1, budget_usd=Decimal("1"))
    assert r1 is expected
    assert r2 is expected
    assert len(agent.calls) == 2


def test_mock_sequence_consumes_one_per_call() -> None:
    agent = MockCodingAgent(
        sequence=[
            ProposalResult(diff="first"),
            ProposalResult(diff="second"),
        ]
    )
    a = agent.propose(worktree=Path("/tmp"), prompt="x", max_turns=1, budget_usd=Decimal("1"))
    b = agent.propose(worktree=Path("/tmp"), prompt="y", max_turns=1, budget_usd=Decimal("1"))
    assert a.diff == "first"
    assert b.diff == "second"
    with pytest.raises(IndexError):
        agent.propose(worktree=Path("/tmp"), prompt="z", max_turns=1, budget_usd=Decimal("1"))


def test_mock_callable_modifies_worktree(repo: Path) -> None:
    """Callable-Modus: Funktion modifiziert Worktree, Mock baut Diff aus git."""

    def fix(wt: Path, prompt: str) -> None:  # gibt nichts zurück
        path = wt / "src" / "calc.py"
        path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return None

    agent = MockCodingAgent(callable_=fix)
    result = agent.propose(
        worktree=repo,
        prompt="fix add",
        max_turns=1,
        budget_usd=Decimal("1"),
    )
    assert "+    return a + b" in result.diff
    assert "-    return a - b" in result.diff
    assert result.has_changes is True
    assert result.stop_reason == "end_turn"


def test_mock_callable_can_return_explicit_result(repo: Path) -> None:
    explicit = ProposalResult(
        diff="custom-diff",
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.01"),
    )

    def fn(wt: Path, prompt: str) -> ProposalResult:
        return explicit

    agent = MockCodingAgent(callable_=fn)
    out = agent.propose(
        worktree=repo, prompt="x", max_turns=1, budget_usd=Decimal("1")
    )
    assert out is explicit


def test_mock_rejects_multiple_modes() -> None:
    with pytest.raises(ValueError):
        MockCodingAgent(
            static_result=ProposalResult(diff=""),
            sequence=[ProposalResult(diff="")],
        )


def test_mock_satisfies_protocol() -> None:
    agent: CodingAgent = MockCodingAgent(static_result=ProposalResult(diff=""))
    assert agent is not None  # type-only assertion via assignment


# --- ClaudeCodeCLIAgent -------------------------------------------------


def test_claude_cli_raises_on_missing_binary(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    agent = ClaudeCodeCLIAgent(claude_bin="this-binary-does-not-exist-99999")
    with pytest.raises(CodingAgentError, match="not found"):
        agent.propose(
            worktree=repo,
            prompt="x",
            max_turns=1,
            budget_usd=Decimal("1"),
        )


def test_claude_cli_satisfies_protocol() -> None:
    agent: CodingAgent = ClaudeCodeCLIAgent()
    assert agent is not None
