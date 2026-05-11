"""Tests für forge_adapters.github.pr."""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from forge_adapters.github.pr import (
    GitHubError,
    _extract_pr_number,
    queue_auto_merge,
    render_pr_body,
    repo_supports_auto_merge,
)


def test_render_pr_body_minimal() -> None:
    body = render_pr_body(
        run_id="01HZX001",
        focus="legacy_test_revival",
        decision="pr_created",
        final_score=0.81,
        score_delta=0.04,
        total_cost_usd=Decimal("0.94"),
        files_changed=["src/calc.py"],
        generations_count=3,
        factory_version="git:abc123",
    )
    assert "## forge auto-PR" in body
    assert "01HZX001" in body
    assert "legacy_test_revival" in body
    assert "0.8100" in body or "0.8100" in body or "0.81" in body
    assert "+0.0400" in body or "0.04" in body
    assert "src/calc.py" in body
    assert "git:abc123" in body
    assert "Auto-merge is categorically disabled" in body


def test_render_pr_body_no_focus_no_delta() -> None:
    body = render_pr_body(
        run_id="r1",
        focus=None,
        decision="pr_created",
        final_score=None,
        score_delta=None,
        total_cost_usd=Decimal("0"),
        files_changed=[],
        generations_count=1,
        factory_version="pkg:0.1.0",
    )
    assert "manual" in body
    assert "—" in body  # placeholder for missing values


def test_render_pr_body_truncates_files_changed() -> None:
    files = [f"src/file_{i}.py" for i in range(40)]
    body = render_pr_body(
        run_id="r1",
        focus="x",
        decision="pr_created",
        final_score=0.5,
        score_delta=0.01,
        total_cost_usd=Decimal("0"),
        files_changed=files,
        generations_count=1,
        factory_version="git:abc",
    )
    assert "src/file_29.py" in body
    assert "and 10 more" in body


def test_render_pr_body_includes_diff_excerpt() -> None:
    body = render_pr_body(
        run_id="r1",
        focus="x",
        decision="pr_created",
        final_score=0.5,
        score_delta=0.01,
        total_cost_usd=Decimal("0"),
        files_changed=["a.py"],
        generations_count=1,
        factory_version="git:abc",
        diff_excerpt="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    assert "```diff" in body
    assert "+++ b/a.py" in body


def test_extract_pr_number_from_url() -> None:
    assert _extract_pr_number("https://github.com/owner/repo/pull/42") == 42
    assert _extract_pr_number(
        "Creating pull request...\nhttps://github.com/owner/repo/pull/123\n"
    ) == 123


def test_extract_pr_number_invalid() -> None:
    with pytest.raises(GitHubError):
        _extract_pr_number("no number here")


# --- queue_auto_merge --------------------------------------------------


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr
    )


def _fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr
    )


def _stub_runner(*responses: subprocess.CompletedProcess):
    iterator = iter(responses)
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return next(iterator)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_repo_supports_auto_merge_true(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),  # nameWithOwner lookup
        _ok(stdout="true\n"),  # graphql autoMergeAllowed
    )
    assert (
        repo_supports_auto_merge(repo=tmp_path, run_subprocess=runner) is True
    )
    # Verify GraphQL flow used
    assert "graphql" in runner.calls[1]  # type: ignore[attr-defined]


def test_repo_supports_auto_merge_false(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),
        _ok(stdout="false\n"),
    )
    assert (
        repo_supports_auto_merge(repo=tmp_path, run_subprocess=runner) is False
    )


def test_repo_supports_auto_merge_slug_lookup_failure_raises(tmp_path: Path) -> None:
    runner = _stub_runner(_fail("HTTP 401: Bad credentials"))
    with pytest.raises(GitHubError, match="nameWithOwner failed"):
        repo_supports_auto_merge(repo=tmp_path, run_subprocess=runner)


def test_repo_supports_auto_merge_graphql_failure_raises(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),
        _fail("rate limit exceeded"),
    )
    with pytest.raises(GitHubError, match="autoMergeAllowed failed"):
        repo_supports_auto_merge(repo=tmp_path, run_subprocess=runner)


def test_queue_auto_merge_happy_path_squash(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),  # nameWithOwner
        _ok(stdout="true\n"),           # graphql autoMergeAllowed
        _ok(),                           # gh pr merge --auto --squash --delete-branch
    )
    queue_auto_merge(repo=tmp_path, pr_number=42, run_subprocess=runner)

    merge_cmd = runner.calls[2]  # type: ignore[attr-defined]
    assert "merge" in merge_cmd
    assert "42" in merge_cmd
    assert "--auto" in merge_cmd
    assert "--squash" in merge_cmd
    assert "--delete-branch" in merge_cmd


def test_queue_auto_merge_supports_method_override(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),
        _ok(stdout="true\n"),
        _ok(),
    )
    queue_auto_merge(
        repo=tmp_path,
        pr_number=7,
        method="rebase",
        delete_branch=False,
        run_subprocess=runner,
    )
    merge_cmd = runner.calls[2]  # type: ignore[attr-defined]
    assert "--rebase" in merge_cmd
    assert "--delete-branch" not in merge_cmd


def test_queue_auto_merge_raises_when_feature_disabled(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),
        _ok(stdout="false\n"),
    )
    with pytest.raises(GitHubError, match="auto-merge disabled"):
        queue_auto_merge(repo=tmp_path, pr_number=42, run_subprocess=runner)


def test_queue_auto_merge_raises_on_gh_merge_failure(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(stdout="rudi77/pinta\n"),
        _ok(stdout="true\n"),
        _fail("Pull request not found"),
    )
    with pytest.raises(GitHubError, match="gh pr merge --auto failed"):
        queue_auto_merge(repo=tmp_path, pr_number=999, run_subprocess=runner)
