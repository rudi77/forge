"""Tests für forge_adapters.github.pr."""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from forge_adapters.github.pr import (
    GitHubError,
    _extract_pr_number,
    create_release,
    fetch_pr_head_committed_at,
    fetch_pr_metadata,
    gh_current_login,
    merge_pr,
    post_pr_review,
    queue_auto_merge,
    render_pr_body,
    repo_supports_auto_merge,
    summarize_ci,
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
    assert "after an agent review" in body


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


def test_render_pr_body_includes_short_plan_inline() -> None:
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
        plan_md="## Plan\n\n1. Fix the bug in a.py\n2. Add regression test",
    )
    assert "### Plan" in body
    assert "Fix the bug in a.py" in body
    # Kurze Pläne werden NICHT eingeklappt.
    assert "<details><summary>Plan (full)</summary>" not in body


def test_render_pr_body_collapses_long_plan() -> None:
    long_plan = "## Plan\n\n" + "- step description that drags on\n" * 200
    assert len(long_plan) > 2000
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
        plan_md=long_plan,
    )
    assert "### Plan" in body
    assert "<details><summary>Plan (full)</summary>" in body
    assert "</details>" in body


def test_render_pr_body_omits_plan_section_when_absent() -> None:
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
    )
    assert "### Plan" not in body


def test_render_pr_body_omits_plan_section_for_whitespace_only() -> None:
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
        plan_md="   \n\n  \t  \n",
    )
    assert "### Plan" not in body


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


# --- summarize_ci (rein) ----------------------------------------------------


def test_summarize_ci_empty_is_none() -> None:
    assert summarize_ci([]) == "none"


def test_summarize_ci_all_success_checkruns() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "SKIPPED"},
    ]
    assert summarize_ci(rollup) == "pass"


def test_summarize_ci_failure_dominates() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "COMPLETED", "conclusion": "FAILURE"},
        {"status": "IN_PROGRESS", "conclusion": ""},
    ]
    assert summarize_ci(rollup) == "fail"


def test_summarize_ci_pending_when_not_completed() -> None:
    rollup = [
        {"status": "COMPLETED", "conclusion": "SUCCESS"},
        {"status": "IN_PROGRESS", "conclusion": ""},
    ]
    assert summarize_ci(rollup) == "pending"


def test_summarize_ci_status_context_shape() -> None:
    # StatusContext nutzt `state` statt status/conclusion.
    assert summarize_ci([{"state": "SUCCESS"}]) == "pass"
    assert summarize_ci([{"state": "FAILURE"}]) == "fail"
    assert summarize_ci([{"state": "PENDING"}]) == "pending"


# --- fetch_pr_metadata / merge_pr / post_pr_review --------------------------


def test_fetch_pr_metadata_parses_json(tmp_path: Path) -> None:
    payload = (
        '{"number": 12, "title": "Fix bug", "body": "Closes #3", '
        '"state": "OPEN", "baseRefName": "main", "headRefName": "forge/x", '
        '"mergeable": "MERGEABLE", '
        '"statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}]}'
    )
    runner = _stub_runner(_ok(stdout=payload))
    meta = fetch_pr_metadata(repo=tmp_path, pr_number=12, run_subprocess=runner)
    assert meta.number == 12
    assert meta.state == "OPEN"
    assert meta.ci_status == "pass"
    assert meta.mergeable == "MERGEABLE"


def test_merge_pr_happy_path_squash(tmp_path: Path) -> None:
    runner = _stub_runner(
        _ok(),                      # gh pr merge
        _ok(stdout="forgebot\n"),   # gh api user (merger login)
    )
    res = merge_pr(repo=tmp_path, pr_number=42, run_subprocess=runner)
    assert res.merged is True
    assert res.merger == "forgebot"
    assert res.method == "squash"
    merge_cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert "merge" in merge_cmd and "42" in merge_cmd
    assert "--squash" in merge_cmd and "--delete-branch" in merge_cmd
    assert "--auto" not in merge_cmd  # synchroner Merge, kein queue


def test_merge_pr_raises_on_failure(tmp_path: Path) -> None:
    runner = _stub_runner(_fail("not mergeable"))
    with pytest.raises(GitHubError, match="gh pr merge 9 failed"):
        merge_pr(repo=tmp_path, pr_number=9, run_subprocess=runner)


def test_gh_current_login_fallback(tmp_path: Path) -> None:
    runner = _stub_runner(_fail("no auth"))
    assert gh_current_login(repo=tmp_path, run_subprocess=runner) == "forge"


def test_post_pr_review_approve(tmp_path: Path) -> None:
    runner = _stub_runner(_ok())
    post_pr_review(
        repo=tmp_path, pr_number=5, approve=True, body="lgtm", run_subprocess=runner
    )
    cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert "review" in cmd and "--approve" in cmd


def test_post_pr_review_request_changes_raises_on_failure(tmp_path: Path) -> None:
    runner = _stub_runner(_fail("cannot review own pr"))
    with pytest.raises(GitHubError, match="gh pr review"):
        post_pr_review(
            repo=tmp_path, pr_number=5, approve=False, body="no", run_subprocess=runner
        )


# --- fetch_pr_head_committed_at (A2) --------------------------------------


def test_fetch_pr_head_committed_at_takes_last_commit(tmp_path: Path) -> None:
    # gh pr view --json commits liefert oldest-first; das letzte = Head.
    payload = (
        '{"commits": ['
        '{"committedDate": "2026-06-01T10:00:00Z"},'
        '{"committedDate": "2026-06-01T13:30:00Z"}'
        ']}'
    )
    runner = _stub_runner(_ok(stdout=payload))
    ts = fetch_pr_head_committed_at(repo=tmp_path, pr_number=7, run_subprocess=runner)
    assert ts is not None
    assert ts.year == 2026 and ts.hour == 13 and ts.minute == 30
    assert ts.tzinfo is not None  # tz-aware (UTC)
    cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert cmd[:4] == ["gh", "pr", "view", "7"]
    assert "commits" in cmd


def test_fetch_pr_head_committed_at_fail_open(tmp_path: Path) -> None:
    # gh-Fehler → None (fail-open, kein raise → Tick wedged nicht).
    assert (
        fetch_pr_head_committed_at(
            repo=tmp_path, pr_number=7, run_subprocess=_stub_runner(_fail("boom"))
        )
        is None
    )
    # Müll-JSON → None.
    assert (
        fetch_pr_head_committed_at(
            repo=tmp_path, pr_number=7, run_subprocess=_stub_runner(_ok(stdout="not json"))
        )
        is None
    )
    # Leere Commit-Liste → None.
    assert (
        fetch_pr_head_committed_at(
            repo=tmp_path, pr_number=7, run_subprocess=_stub_runner(_ok(stdout='{"commits": []}'))
        )
        is None
    )


# --- create_release (B2) --------------------------------------------------


def test_create_release_happy_path(tmp_path: Path) -> None:
    runner = _stub_runner(
        _fail("release not found", code=1),  # gh release view (does not exist yet)
        _ok(stdout="https://github.com/o/r/releases/tag/forge-issue-7\n"),  # create
    )
    url = create_release(
        repo=tmp_path, tag="forge-issue-7", title="t", run_subprocess=runner
    )
    assert url == "https://github.com/o/r/releases/tag/forge-issue-7"
    create_cmd = runner.calls[1]  # type: ignore[attr-defined]
    assert create_cmd[:4] == ["gh", "release", "create", "forge-issue-7"]
    assert "--generate-notes" in create_cmd


def test_create_release_idempotent_when_tag_exists(tmp_path: Path) -> None:
    # Tag existiert bereits → bestehende URL zurück, kein zweiter create-Call.
    runner = _stub_runner(
        _ok(stdout='{"url": "https://github.com/o/r/releases/tag/forge-issue-7"}'),
    )
    url = create_release(
        repo=tmp_path, tag="forge-issue-7", title="t", run_subprocess=runner
    )
    assert url == "https://github.com/o/r/releases/tag/forge-issue-7"
    assert len(runner.calls) == 1  # type: ignore[attr-defined]  # nur view, kein create


def test_create_release_raises_on_real_failure(tmp_path: Path) -> None:
    runner = _stub_runner(
        _fail("release not found", code=1),  # view → not exists
        _fail("boom", code=1),               # create → genuine failure
    )
    with pytest.raises(GitHubError, match="gh release create"):
        create_release(
            repo=tmp_path, tag="forge-issue-7", title="t", run_subprocess=runner
        )
