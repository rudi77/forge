"""Tests für forge_execute.pr_review — decide_merge (rein) + PRReviewer (fail-closed)."""

from __future__ import annotations

from pathlib import Path

from forge_execute.agents.base import CodingAgentError, CodingAgentTimeout, ReviewResult
from forge_execute.agents.mock import MockCodingAgent
from forge_execute.pr_review import PRReviewer, decide_merge

# --- decide_merge (rein) ----------------------------------------------------


def test_decide_merge_happy_path() -> None:
    d = decide_merge(
        verdict="approve",
        score=0.9,
        ci_status="pass",
        threshold=0.8,
        capability_enabled=True,
    )
    assert d.merge is True
    assert d.reason is None


def test_decide_merge_blocked_when_capability_disabled() -> None:
    d = decide_merge(
        verdict="approve", score=1.0, ci_status="pass", threshold=0.8,
        capability_enabled=False,
    )
    assert d.merge is False
    assert d.reason == "capability_disabled"


def test_decide_merge_blocked_on_request_changes() -> None:
    d = decide_merge(
        verdict="request_changes", score=0.95, ci_status="pass", threshold=0.8,
        capability_enabled=True,
    )
    assert d.merge is False
    assert d.reason == "review_requested_changes"


def test_decide_merge_blocked_below_threshold() -> None:
    d = decide_merge(
        verdict="approve", score=0.5, ci_status="pass", threshold=0.8,
        capability_enabled=True,
    )
    assert d.merge is False
    assert d.reason == "below_threshold"


def test_decide_merge_blocked_when_ci_not_green() -> None:
    d = decide_merge(
        verdict="approve", score=0.9, ci_status="fail", threshold=0.8,
        capability_enabled=True,
    )
    assert d.merge is False
    assert d.reason == "ci_not_green:fail"


def test_decide_merge_missing_ci_allowed_by_default() -> None:
    d = decide_merge(
        verdict="approve", score=0.9, ci_status="none", threshold=0.8,
        capability_enabled=True,
    )
    assert d.merge is True


def test_decide_merge_missing_ci_blocked_when_required() -> None:
    d = decide_merge(
        verdict="approve", score=0.9, ci_status="none", threshold=0.8,
        capability_enabled=True, allow_missing_ci=False,
    )
    assert d.merge is False
    assert d.reason == "ci_not_green:none"


# --- PRReviewer (fail-closed) -----------------------------------------------


def test_pr_reviewer_maps_pass_to_approve(tmp_path: Path) -> None:
    agent = MockCodingAgent(
        callable_=lambda wt, p: None,
        review_result=ReviewResult(judge_score=0.92, verdict="pass", reasoning="ok"),
    )
    reviewer = PRReviewer(agent)
    out = reviewer.review_open_pr(
        repo_root=tmp_path,
        pr_title="t",
        pr_body="b",
        pr_diff="diff",
        ci_status="pass",
    )
    assert out.verdict == "approve"
    assert out.approved is True
    assert out.score == 0.92


def test_pr_reviewer_maps_fail_to_request_changes(tmp_path: Path) -> None:
    agent = MockCodingAgent(
        callable_=lambda wt, p: None,
        review_result=ReviewResult(judge_score=0.3, verdict="fail", reasoning="nope"),
    )
    out = PRReviewer(agent).review_open_pr(
        repo_root=tmp_path, pr_title="t", pr_body="b", pr_diff="d", ci_status="pass"
    )
    assert out.verdict == "request_changes"
    assert out.approved is False


def test_pr_reviewer_fail_closed_on_agent_error(tmp_path: Path) -> None:
    def boom(*_a, **_k):
        raise CodingAgentError("crash")

    agent = MockCodingAgent(callable_=lambda wt, p: None, review_callable=boom)
    out = PRReviewer(agent).review_open_pr(
        repo_root=tmp_path, pr_title="t", pr_body="b", pr_diff="d", ci_status="pass"
    )
    assert out.verdict == "request_changes"
    assert out.score == 0.0
    assert out.error is not None


def test_pr_reviewer_fail_closed_on_timeout(tmp_path: Path) -> None:
    def slow(*_a, **_k):
        raise CodingAgentTimeout("too slow")

    agent = MockCodingAgent(callable_=lambda wt, p: None, review_callable=slow)
    out = PRReviewer(agent).review_open_pr(
        repo_root=tmp_path, pr_title="t", pr_body="b", pr_diff="d", ci_status="pass"
    )
    assert out.verdict == "request_changes"
    assert out.score == 0.0
