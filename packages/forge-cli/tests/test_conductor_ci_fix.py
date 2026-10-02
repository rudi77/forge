"""L2: CI-Autofix im Conductor (Signale, State-Machine, Wiring)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli.conductor import DispatchOrder, WorkItem, derive_signals, plan_tick
from forge_cli.runtime import ForgeContext
from forge_cli.stages import MAX_CI_FIX_ATTEMPTS, Stage, StageSignals, advance
from forge_core.events import EventKind as EK
from forge_core.spec import BoardConfig, CostCapsConfig, ProjectSpec, TriageConfig
from forge_core.tracking import ReadyIssue


class _Evt:
    def __init__(self, kind, run_id, payload, ts):
        self.kind, self.run_id, self.payload, self.ts = kind, run_id, payload, ts


def _t(hour: int) -> datetime:
    return datetime(2026, 6, 1, hour, tzinfo=UTC)


def _events() -> list[_Evt]:
    return [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}, _t(1)),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}, _t(2)),
    ]


def _ci_run(run_id: str, hour: int, decision: str | None = None) -> list[_Evt]:
    out = [_Evt(EK.RUN_STARTED, run_id, {"issue_number": 42, "trigger": "ci_failure",
                                         "pr_number": 100}, _t(hour))]
    if decision:
        out.append(_Evt(EK.RUN_FINISHED, run_id, {"decision": decision}, _t(hour + 1)))
    return out


# --- Signale -------------------------------------------------------------------


def test_ci_status_is_passed_through() -> None:
    sig = derive_signals(_events(), 42, ci_status="fail")
    assert sig.ci_failed is True
    assert sig.ci_fix_attempts == 0
    assert sig.ci_fix_started is False


def test_ci_fix_for_current_head_counts_as_started() -> None:
    events = _events() + _ci_run("c1", 5)
    sig = derive_signals(events, 42, head_committed_at=_t(3), ci_status="fail")
    assert sig.ci_fix_started is True and sig.ci_fix_attempts == 1
    # Ein neuerer Head (Fix gepusht, CI wieder rot) → nicht mehr "started".
    sig2 = derive_signals(events, 42, head_committed_at=_t(7), ci_status="fail")
    assert sig2.ci_fix_started is False


def test_failed_ci_fix_run_is_reported() -> None:
    events = _events() + _ci_run("c1", 5, "no_improvement")
    sig = derive_signals(events, 42, head_committed_at=_t(3), ci_status="fail")
    assert sig.ci_fix_failed is True


def test_without_head_timestamp_only_inflight_or_failed_counts() -> None:
    done_ok = _events() + _ci_run("c1", 5, "pr_created")
    assert derive_signals(done_ok, 42, ci_status="fail").ci_fix_started is False
    in_flight = _events() + _ci_run("c1", 5)
    assert derive_signals(in_flight, 42, ci_status="fail").ci_fix_started is True


# --- State-Machine ---------------------------------------------------------------


def test_qa_with_red_ci_goes_back_to_in_dev() -> None:
    sig = StageSignals(has_open_pr=True, ci_status="fail")
    assert advance(Stage.QA, sig) == (Stage.IN_DEV, "ci_failed")


def test_qa_with_red_ci_blocked_after_max_attempts() -> None:
    sig = StageSignals(has_open_pr=True, ci_status="fail", ci_fix_attempts=MAX_CI_FIX_ATTEMPTS)
    assert advance(Stage.QA, sig) == (Stage.BLOCKED, "ci_fix_exhausted")


def test_in_dev_with_red_ci_does_not_advance_to_qa() -> None:
    assert advance(Stage.IN_DEV, StageSignals(has_open_pr=True, ci_status="fail")) == (
        Stage.IN_DEV, "",
    )


def test_qa_review_waits_for_pending_ci() -> None:
    item = WorkItem(42, Stage.QA, signals=StageSignals(has_open_pr=True, ci_status="pending"))
    plan = plan_tick([item], capacity=1)
    assert plan.dispatch == [] and plan.transitions == []


def test_qa_review_dispatched_on_green_ci() -> None:
    item = WorkItem(42, Stage.QA, signals=StageSignals(has_open_pr=True, ci_status="pass"))
    assert plan_tick([item], capacity=1).dispatch == [DispatchOrder(42, Stage.QA)]


def test_plan_tick_dispatches_ci_fix() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(has_open_pr=True, ci_status="fail"))
    assert plan_tick([item], capacity=1).dispatch == [
        DispatchOrder(42, Stage.IN_DEV, "ci_fix")
    ]


def test_plan_tick_ci_fix_failed_escalates() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, ci_status="fail", ci_fix_started=True, ci_fix_failed=True,
        ci_fix_attempts=1))
    plan = plan_tick([item], capacity=1)
    assert [b.kind for b in plan.blocked] == ["ci_fix_exhausted"]
    assert plan.transitions[0].to_stage == Stage.BLOCKED


def test_rework_has_precedence_over_ci_fix() -> None:
    item = WorkItem(42, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, ci_status="fail", changes_requested=True))
    assert plan_tick([item], capacity=1).dispatch[0].kind == "rework"


# --- Wiring ----------------------------------------------------------------------


def _ctx(tmp_path: Path) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0", name="p",
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


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )


def test_conductor_red_ci_triggers_ci_fix_with_failure_context(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from forge_core.events import PRCreatedPayload, RunStartedPayload, build_event

    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/r8", title="t", body="b", push=False)
    ctx.code_host.prs[pr.pr_number].ci_status = "fail"
    ctx.code_host.prs[pr.pr_number].ci_failure = "Failing checks:\n- tests"
    ctx.tracker.add(ReadyIssue(number=8, title="t", body="b", labels=["forge:qa"],
                               project_status="", url=""))
    common = dict(project="p", project_fingerprint="sha256:test",
                  factory_version="git:test", spec_version="1.0")
    store = ctx.open_store()
    store.append(build_event(kind=EK.RUN_STARTED, run_id="r8", payload=RunStartedPayload(
        trigger="issue_label", strategy="sequential", config_hash="c", issue_number=8),
        **common))
    store.append(build_event(kind=EK.PR_CREATED, run_id="r8", payload=PRCreatedPayload(
        pr_number=pr.pr_number, branch="forge/r8"), **common))
    store.close()

    calls: list[dict] = []
    monkeypatch.setattr(bl, "_dispatch_branch_run", lambda **kw: calls.append(kw) or
                        bl._PassResult(summaries=[], bailed=False, dispatched=1, skipped=0))
    monkeypatch.setattr(bl, "_dispatch_review_run", lambda **kw: (_ for _ in ()).throw(
        AssertionError("no review on red CI")))
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=2)
    assert ctx.tracker.stage_calls[0] == (8, "forge:in-dev", "forge:qa")
    assert calls and calls[0]["kind"] == "ci_fix"
    assert "Failing checks" in calls[0]["context"]


def test_ci_fix_run_uses_ci_failure_trigger_and_roster(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/orig", title="t", body="b", push=False)
    seen: dict[str, Any] = {}
    monkeypatch.setattr(bl.WorktreeManager, "fetch_remote_branch",
                        lambda self, b, remote="origin": "ref")

    def fake(**kw):
        seen.update(kw)
        return type("O", (), {"result": type("R", (), {"decision": "pr_created",
                                                        "branch": "forge/fix"})()})()

    monkeypatch.setattr(bl, "execute_run", fake)
    bl._dispatch_branch_run(ctx=ctx, issue=ReadyIssue(number=1, title="t", body="b",
                            labels=[], project_status="", url=""),
                            pr_number=pr.pr_number, params=_params(), kind="ci_fix",
                            context="tests failed")
    assert seen["trigger"] == "ci_failure"
    assert seen["focus"] == "ci-fix:#1"
    assert seen["agents"] == ["developer"]
    assert "BEGIN UNTRUSTED CI FAILURE" in seen["rendered_prompt"]
    assert ctx.code_host.pushed == ["forge/fix:forge/orig"]
