"""Tests für forge_execute.triage.base + Noop + gh-Helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from forge_adapters.github import ReadyIssue
from forge_execute.triage import NoopTriager, TriageResult, close_issue, comment_issue
from forge_execute.triage.gh import GHTriageError


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


def test_comment_issue_calls_gh_correctly(tmp_path: Path) -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    comment_issue(
        repo=tmp_path,
        issue_number=7,
        body="hello from forge",
        run_subprocess=runner,
    )
    runner.assert_called_once()
    cmd = runner.call_args.args[0]
    assert cmd[:4] == ["gh", "issue", "comment", "7"]
    assert "hello from forge" in cmd


def test_comment_issue_raises_on_gh_error(tmp_path: Path) -> None:
    runner = MagicMock(
        return_value=subprocess.CompletedProcess([], 1, "", "boom")
    )
    with pytest.raises(GHTriageError, match="boom"):
        comment_issue(
            repo=tmp_path,
            issue_number=7,
            body="ignored",
            run_subprocess=runner,
        )


def test_close_issue_passes_reason_flag(tmp_path: Path) -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    close_issue(
        repo=tmp_path,
        issue_number=11,
        reason="not planned",
        run_subprocess=runner,
    )
    cmd = runner.call_args.args[0]
    assert "--reason" in cmd
    idx = cmd.index("--reason")
    assert cmd[idx + 1] == "not planned"


def test_close_issue_defaults_reason_to_not_planned(tmp_path: Path) -> None:
    runner = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    close_issue(repo=tmp_path, issue_number=11, run_subprocess=runner)
    cmd = runner.call_args.args[0]
    idx = cmd.index("--reason")
    assert cmd[idx + 1] == "not planned"


def test_close_issue_raises_on_gh_error(tmp_path: Path) -> None:
    runner = MagicMock(
        return_value=subprocess.CompletedProcess([], 1, "", "no auth")
    )
    with pytest.raises(GHTriageError):
        close_issue(repo=tmp_path, issue_number=1, run_subprocess=runner)
