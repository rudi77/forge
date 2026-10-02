"""L1: Nacharbeits-Loop ``qa → in-dev`` bei ``request_changes`` (rein + Wiring)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli.conductor import DispatchOrder, WorkItem, derive_signals, plan_tick
from forge_cli.runtime import ForgeContext
from forge_cli.stages import MAX_REWORK_ROUNDS, Stage, StageSignals, advance
from forge_core.events import EventKind as EK
from forge_core.spec import BoardConfig, CostCapsConfig, ProjectSpec, TriageConfig
from forge_core.tracking import ReadyIssue


class _Evt:
    def __init__(self, kind, run_id, payload, ts):
        self.kind = kind
        self.run_id = run_id
        self.payload = payload
        self.ts = ts


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def _base_events(*reviews: tuple[str, int]) -> list[_Evt]:
    """Issue #42, Dev-Run r1 hat PR #100 geöffnet, dann Reviews (verdict, hour)."""
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}, _t(1)),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}, _t(2)),
    ]
    for i, (verdict, hour) in enumerate(reviews):
        events.append(
            _Evt(EK.PR_REVIEWED, f"rev{i}", {"pr_number": 100, "verdict": verdict}, _t(hour))
        )
    return events


# --- derive_signals ----------------------------------------------------------


def test_request_changes_sets_changes_requested_and_rounds() -> None:
    sig = derive_signals(_base_events(("request_changes", 3)), 42)
    assert sig.changes_requested is True
    assert sig.rework_rounds == 1
    assert sig.rework_started is False


def test_new_commit_after_review_clears_changes_requested() -> None:
    events = _base_events(("request_changes", 3))
    assert derive_signals(events, 42, head_committed_at=_t(4)).changes_requested is False
    assert derive_signals(events, 42, head_committed_at=_t(2)).changes_requested is True


def test_approve_is_not_changes_requested() -> None:
    sig = derive_signals(_base_events(("request_changes", 3), ("approve", 5)), 42)
    assert sig.changes_requested is False
    assert sig.rework_rounds == 1


def test_rework_run_since_review_is_tracked() -> None:
    events = _base_events(("request_changes", 3))
    events.append(_Evt(EK.RUN_STARTED, "rw1", {"issue_number": 42, "trigger": "rework",
                                               "pr_number": 100}, _t(4)))
    sig = derive_signals(events, 42)
    assert sig.rework_started is True
    assert sig.rework_failed is False
    events.append(_Evt(EK.RUN_FINISHED, "rw1", {"decision": "no_improvement"}, _t(5)))
    assert derive_signals(events, 42).rework_failed is True


def test_rework_run_before_latest_review_does_not_count() -> None:
    events = _base_events(("request_changes", 3))
    events.append(_Evt(EK.RUN_STARTED, "rw1", {"issue_number": 42, "trigger": "rework"}, _t(4)))
    events.append(_Evt(EK.RUN_FINISHED, "rw1", {"decision": "pr_created"}, _t(5)))
    events.append(
        _Evt(EK.PR_REVIEWED, "rev9", {"pr_number": 100, "verdict": "request_changes"}, _t(6))
    )
    sig = derive_signals(events, 42)
    assert sig.rework_rounds == 2
    assert sig.rework_started is False


def test_rework_runs_are_not_dev_failures() -> None:
    from forge_cli.conductor import derive_dev_failure

    events = _base_events(("request_changes", 3))
    events.append(_Evt(EK.RUN_STARTED, "rw1", {"issue_number": 42, "trigger": "rework",
                                               "pr_number": 100, "focus": "rework:#42"}, _t(4)))
    events.append(_Evt(EK.RUN_FINISHED, "rw1", {"decision": "no_improvement"}, _t(5)))
    assert derive_dev_failure(events, 42) == (False, 0)


# --- advance + plan_tick -------------------------------------------------------


def test_advance_qa_back_to_in_dev_on_changes_requested() -> None:
    sig = StageSignals(has_open_pr=True, changes_requested=True, rework_rounds=1)
    assert advance(Stage.QA, sig) == (Stage.IN_DEV, "review_changes_requested")


def test_advance_qa_blocked_after_max_rounds() -> None:
    sig = StageSignals(
        has_open_pr=True, changes_requested=True, rework_rounds=MAX_REWORK_ROUNDS + 1
    )
    assert advance(Stage.QA, sig) == (Stage.BLOCKED, "rework_exhausted")


def test_in_dev_with_open_pr_stays_while_changes_requested() -> None:
    sig = StageSignals(has_open_pr=True, changes_requested=True)
    assert advance(Stage.IN_DEV, sig) == (Stage.IN_DEV, "")
    assert advance(Stage.IN_DEV, StageSignals(has_open_pr=True)) == (Stage.QA, "pr_created")


def test_plan_tick_dispatches_rework_without_stage_transition() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(has_open_pr=True,
                                                           changes_requested=True))
    plan = plan_tick([item], capacity=1)
    assert plan.dispatch == [DispatchOrder(42, Stage.IN_DEV, "rework")]
    assert plan.transitions == []


def test_plan_tick_rework_started_waits() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, changes_requested=True, rework_started=True))
    plan = plan_tick([item], capacity=1)
    assert plan.dispatch == [] and plan.transitions == []


def test_plan_tick_rework_failed_escalates() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, changes_requested=True, rework_started=True, rework_failed=True))
    plan = plan_tick([item], capacity=1)
    assert plan.dispatch == []
    assert [(t.to_stage, t.reason) for t in plan.transitions] == [
        (Stage.BLOCKED, "rework_no_change")
    ]
    assert [b.kind for b in plan.blocked] == ["rework_no_change"]


def test_plan_tick_rework_exhausted_reports_blocked() -> None:
    item = WorkItem(42, Stage.QA, signals=StageSignals(
        has_open_pr=True, changes_requested=True, rework_rounds=MAX_REWORK_ROUNDS + 1))
    plan = plan_tick([item], capacity=1)
    assert [b.kind for b in plan.blocked] == ["rework_exhausted"]
    assert plan.dispatch == []


# --- Wiring: _dispatch_branch_run --------------------------------------------


def _ctx(tmp_path: Path) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0",
        name="p",
        cost_caps=CostCapsConfig(
            per_generation_usd=Decimal("0.5"), per_run_usd=Decimal("2"),
            per_project_per_day_usd=Decimal("10"), per_project_per_month_usd=Decimal("50"),
        ),
        board=BoardConfig(owner="x", project_number=1),
        triage=TriageConfig(enabled=False),
    )
    return ForgeContext(
        repo_root=tmp_path, forge_dir=tmp_path / ".forge", spec=spec,
        spec_path=tmp_path / ".forge" / "project.yaml", factory_version="git:test",
        project_fingerprint="sha256:test", store_path=tmp_path / "events.duckdb",
        blobs_path=tmp_path / "blobs", tracker=InMemoryTracker(), code_host=InMemoryCodeHost(),
    )


def _issue() -> ReadyIssue:
    return ReadyIssue(number=42, title="t", body="b", labels=["forge:in-dev"],
                      project_status="", url="")


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )


class _Result:
    def __init__(self, decision: str, branch: str | None):
        self.decision = decision
        self.branch = branch


def test_branch_run_pushes_fast_forward_to_pr_head(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/orig", title="t", body="b", push=False)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(bl.WorktreeManager, "fetch_remote_branch",
                        lambda self, b, remote="origin": f"refs/remotes/origin/{b}")

    def fake_execute_run(**kw):
        seen.update(kw)
        return type("O", (), {"result": _Result("pr_created", "forge/rw1")})()

    monkeypatch.setattr(bl, "execute_run", fake_execute_run)
    res = bl._dispatch_branch_run(
        ctx=ctx, issue=_issue(), pr_number=pr.pr_number, params=_params(),
        kind="rework", context="BLOCKING: missing null check",
    )
    assert res.dispatched == 1
    assert ctx.code_host.pushed == ["forge/rw1:forge/orig"]
    assert seen["base_ref"] == "refs/remotes/origin/forge/orig"
    assert seen["trigger"] == "rework"
    assert seen["create_pr"] is False
    assert seen["pr_number"] == pr.pr_number
    assert "BEGIN UNTRUSTED REVIEW FINDINGS" in seen["rendered_prompt"]
    assert "missing null check" in seen["acceptance_criteria"]


def test_branch_run_without_result_pushes_nothing(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/orig", title="t", body="b", push=False)
    monkeypatch.setattr(bl.WorktreeManager, "fetch_remote_branch",
                        lambda self, b, remote="origin": "ref")
    monkeypatch.setattr(bl, "execute_run", lambda **kw: type(
        "O", (), {"result": _Result("no_improvement", "forge/rw1")})())
    bl._dispatch_branch_run(ctx=ctx, issue=_issue(), pr_number=pr.pr_number,
                            params=_params(), kind="rework", context="")
    assert ctx.code_host.pushed == []


def test_branch_run_refuses_human_branch(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="feature/human", title="t", body="b", push=False)
    monkeypatch.setattr(bl, "execute_run", lambda **kw: (_ for _ in ()).throw(
        AssertionError("must not run")))
    res = bl._dispatch_branch_run(ctx=ctx, issue=_issue(), pr_number=pr.pr_number,
                                  params=_params(), kind="rework", context="")
    assert res.skipped == 1
    assert ctx.code_host.pushed == []


def test_conductor_tick_moves_rejected_qa_item_back_and_dispatches_rework(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Zwei Ticks: qa→in-dev (request_changes), dann Rework-Dispatch mit der
    Review-Begründung aus dem Blob-Store."""
    from forge_core.events import (
        PRCreatedPayload,
        PRReviewedPayload,
        RunStartedPayload,
        build_event,
    )

    ctx = _ctx(tmp_path)
    ctx.tracker.add(ReadyIssue(number=8, title="t", body="b", labels=["forge:qa"],
                               project_status="", url=""))
    reasoning_blob = ctx.open_blobs().put_text("BLOCKING: handle empty input")
    common = dict(project="p", project_fingerprint="sha256:test",
                  factory_version="git:test", spec_version="1.0")
    store = ctx.open_store()
    store.append(build_event(kind=EK.RUN_STARTED, run_id="r8", payload=RunStartedPayload(
        trigger="issue_label", strategy="sequential", config_hash="c", issue_number=8),
        **common))
    store.append(build_event(kind=EK.PR_CREATED, run_id="r8",
                             payload=PRCreatedPayload(pr_number=42, branch="forge/r8"), **common))
    store.append(build_event(kind=EK.PR_REVIEWED, run_id="rv", payload=PRReviewedPayload(
        pr_number=42, verdict="request_changes", score=0.2, ci_status="pass", merged=False,
        reasoning_blob=reasoning_blob), **common))
    store.close()

    calls: list[dict] = []

    def fake_branch_run(**kw):
        calls.append(kw)
        return bl._PassResult(summaries=[], bailed=False, dispatched=1, skipped=0)

    monkeypatch.setattr(bl, "_dispatch_branch_run", fake_branch_run)
    stats = bl._run_conductor_watch(
        ctx=ctx, max_issues=3, interval_s=0, params=_params(), triager=None,
        capabilities=None, max_ticks=2,
    )
    assert ctx.tracker.stage_calls[0] == (8, "forge:in-dev", "forge:qa")
    assert stats.total_dispatched == 1
    assert calls[0]["kind"] == "rework"
    assert calls[0]["pr_number"] == 42
    assert "handle empty input" in calls[0]["context"]


def test_fetch_remote_branch_against_real_remote(tmp_path: Path) -> None:
    from forge_execute.worktrees import WorktreeManager

    def git(cwd: Path, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                              text=True).stdout.strip()

    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    git(seed, "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
        "--allow-empty", "-m", "init")
    git(seed, "remote", "add", "origin", str(remote))
    git(seed, "push", "-q", "origin", "main:refs/heads/forge/pr-head")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(remote), str(clone))

    ref = WorktreeManager(clone).fetch_remote_branch("forge/pr-head")
    assert ref == "refs/remotes/origin/forge/pr-head"
    assert git(clone, "rev-parse", ref) == git(seed, "rev-parse", "HEAD")
