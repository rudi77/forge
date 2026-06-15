"""Tests für forge_cli.review_pr.execute_pr_review (End-to-End mit Fakes)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge_cli.review_pr import execute_pr_review
from forge_core.events import EventKind
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from forge_execute.agents.base import ReviewResult
from forge_execute.agents.mock import MockCodingAgent


def _spec(*, merge_pr: bool) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "testproj",
            "cost_caps": {
                "per_generation_usd": "0.5",
                "per_run_usd": "5",
                "per_project_per_day_usd": "30",
                "per_project_per_month_usd": "500",
            },
            "capabilities": {"merge_pr": merge_pr},
        }
    )


@dataclass
class _Ctx:
    repo_root: Path
    spec: ProjectSpec
    project_fingerprint: str = "sha256:test"
    factory_version: str = "git:test"
    store_path: Path = Path()

    def open_store(self) -> EventStore:
        return EventStore(self.store_path)


def _ctx(tmp_path: Path, *, merge_pr: bool) -> _Ctx:
    return _Ctx(repo_root=tmp_path, spec=_spec(merge_pr=merge_pr),
                store_path=tmp_path / "events.duckdb")


def _pr_json(*, mergeable: str, ci: str, no_ci: bool) -> str:
    rollup = "[]" if no_ci else f'[{{"status": "COMPLETED", "conclusion": "{ci}"}}]'
    return (
        f'{{"number": 7, "title": "Fix bug", "body": "Closes #1", "state": "OPEN", '
        f'"baseRefName": "main", "headRefName": "forge/x", "mergeable": "{mergeable}", '
        f'"statusCheckRollup": {rollup}}}'
    )


def _fake_gh(*, mergeable: str = "MERGEABLE", ci: str = "SUCCESS", no_ci: bool = False):
    """run_subprocess-Fake, der nach gh-Subcommand verzweigt."""
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        joined = " ".join(cmd)
        if "pr view" in joined or ("view" in cmd and "pr" in cmd):
            out = _pr_json(mergeable=mergeable, ci=ci, no_ci=no_ci)
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")
        if "diff" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="--- a\n+++ b\n", stderr="")
        if "review" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "merge" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "api" in cmd and "user" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="forgebot\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _agent(score: float, verdict: str) -> MockCodingAgent:
    return MockCodingAgent(
        callable_=lambda wt, p: None,
        review_result=ReviewResult(judge_score=score, verdict=verdict, reasoning="r"),
    )


def _kinds(store_path: Path, kind: EventKind) -> list:
    store = EventStore(store_path)
    try:
        return store.events_by_kind(kind)
    finally:
        store.close()


def test_review_approves_and_merges_when_capability_on(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    runner = _fake_gh(ci="SUCCESS")
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"), run_subprocess=runner, threshold=0.8
    )
    assert out.verdict == "approve"
    assert out.merged is True
    # Events: PRReviewed + PRMerged geschrieben.
    reviewed = _kinds(ctx.store_path, EventKind.PR_REVIEWED)
    merged = _kinds(ctx.store_path, EventKind.PR_MERGED)
    assert len(reviewed) == 1
    assert reviewed[0].payload["merged"] is True
    assert len(merged) == 1
    assert merged[0].payload["merged_by_forge"] is True
    assert merged[0].payload["merge_method"] == "squash"


def test_review_does_not_merge_when_capability_off(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=False)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=_fake_gh(), threshold=0.8,
    )
    assert out.verdict == "approve"
    assert out.merged is False
    assert out.merge_decision.reason == "capability_disabled"
    assert _kinds(ctx.store_path, EventKind.PR_MERGED) == []
    reviewed = _kinds(ctx.store_path, EventKind.PR_REVIEWED)
    assert reviewed[0].payload["merge_blocked_reason"] == "capability_disabled"


def test_review_does_not_merge_when_ci_red(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=_fake_gh(ci="FAILURE"), threshold=0.8,
    )
    assert out.merged is False
    assert out.merge_decision.reason == "ci_not_green:fail"


def test_review_no_ci_blocks_merge_by_default(tmp_path: Path) -> None:
    # Repo ohne Checks (ci_status "none") → kein Merge, solange allow_missing_ci
    # nicht gesetzt ist (Safety-Default: ohne grünen CI wird nicht gemergt).
    ctx = _ctx(tmp_path, merge_pr=True)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=_fake_gh(no_ci=True), threshold=0.8,
    )
    assert out.ci_status == "none"
    assert out.merged is False
    assert out.merge_decision.reason == "ci_not_green:none"


def test_review_no_ci_merges_when_explicitly_allowed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=_fake_gh(no_ci=True), threshold=0.8, allow_missing_ci=True,
    )
    assert out.merged is True


def test_review_request_changes_blocks_merge(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.2, "fail"),
        run_subprocess=_fake_gh(), threshold=0.8,
    )
    assert out.verdict == "request_changes"
    assert out.merged is False


def test_review_conflicting_pr_not_merged(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=_fake_gh(mergeable="CONFLICTING"), threshold=0.8,
    )
    assert out.merged is False
    assert out.merge_decision.reason == "conflicting"


def test_dry_run_emits_no_events_and_no_gh_mutations(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, merge_pr=True)
    runner = _fake_gh()
    out = execute_pr_review(
        ctx, pr_number=7, agent=_agent(0.95, "pass"),
        run_subprocess=runner, threshold=0.8, dry_run=True,
    )
    assert out.merged is False
    # Keine Review/Merge-Subprozesse, keine Events.
    assert not any("review" in c or "merge" in c for c in runner.calls)  # type: ignore[attr-defined]
    assert _kinds(ctx.store_path, EventKind.PR_REVIEWED) == []
