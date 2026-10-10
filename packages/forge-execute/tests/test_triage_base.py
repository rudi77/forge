"""Tests für forge_execute.triage.base + Noop + gh-Helpers."""

from __future__ import annotations

from pathlib import Path

from forge_core.tracking import ReadyIssue
from forge_execute.triage import NoopTriager, TriageResult


def _issue(n: int = 42) -> ReadyIssue:
    return ReadyIssue(
        number=n,
        title=f"test issue {n}",
        body="body text",
        labels=["bug"],
        project_status="Ready",
        url=f"https://github.com/x/y/issues/{n}",
    )


def test_triage_result_is_relevant_helper() -> None:
    assert TriageResult(decision="relevant").is_relevant is True
    assert TriageResult(decision="stale").is_relevant is False


def test_noop_triager_always_returns_relevant(tmp_path: Path) -> None:
    triager = NoopTriager()
    result = triager.triage(issue=_issue(), repo_root=tmp_path)
    assert result.decision == "relevant"
    assert result.is_relevant
