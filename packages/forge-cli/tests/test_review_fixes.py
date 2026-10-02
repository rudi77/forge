"""Regressionstests für die Befunde aus dem Code-Review des Fabrik-Umbaus."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_adapters.base import CodeHostError
from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli.conductor import ResumeOrder, WorkItem, plan_tick
from forge_cli.runtime import ForgeContext
from forge_cli.stages import MARKER_LABELS, Stage, StageSignals
from forge_cli.workgen import intake_blocks, refined_spec_text
from forge_core.events import EventKind as EK
from forge_core.spec import (
    BoardConfig,
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    ReleaseConfig,
    TriageConfig,
)
from forge_core.tracking import ReadyIssue


def _ctx(tmp_path: Path, **kw) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0", name="p",
        cost_caps=CostCapsConfig(per_generation_usd=Decimal("1"), per_run_usd=Decimal("1"),
                                 per_project_per_day_usd=Decimal("10"),
                                 per_project_per_month_usd=Decimal("10")),
        board=BoardConfig(owner="x", project_number=1), triage=TriageConfig(enabled=False),
        **kw,
    )
    return ForgeContext(
        repo_root=tmp_path, forge_dir=tmp_path / ".forge", spec=spec,
        spec_path=tmp_path / ".forge" / "p.yaml", factory_version="git:t",
        project_fingerprint="sha256:t", store_path=tmp_path / "e.duckdb",
        blobs_path=tmp_path / "blobs", tracker=InMemoryTracker(), code_host=InMemoryCodeHost(),
    )


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )


class _Evt:
    def __init__(self, kind, run_id, payload, ts=None):
        self.kind, self.run_id, self.payload = kind, run_id, payload
        self.ts = ts or datetime(2026, 6, 1, tzinfo=UTC)


def _outcome(decision: str, branch: str | None = "forge/rw", blocks=None):
    res = type("R", (), {"decision": decision, "branch": branch, "run_id": "rw",
                         "workitems_blocks": blocks or []})()
    return type("O", (), {"result": res, "pr_url": None, "pr_error": None})()


def test_resumed_rework_run_pushes_to_pr_branch_instead_of_opening_a_pr(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/orig", title="t", body="b", push=False)
    issue = ReadyIssue(8, "t", "b", ["forge:in-dev"], "", "")
    events = [_Evt(EK.RUN_STARTED, "rw", {"issue_number": 8, "trigger": "rework",
                                          "focus": "rework:#8", "pr_number": pr.pr_number})]
    seen: dict = {}
    monkeypatch.setattr(bl, "execute_run", lambda **kw: seen.update(kw) or _outcome("pr_created"))
    order = ResumeOrder(run_id="rw", resume_session_id="s", resume_at=datetime.now(UTC),
                        issue_number=8, worktree="")
    bl._dispatch_resume(ctx=ctx, order=order, params=_params(), issue=issue, events=events)
    assert seen["create_pr"] is False
    assert seen["trigger"] == "rework" and seen["focus"] == "rework:#8"
    assert seen["pr_number"] == pr.pr_number
    assert ctx.code_host.pushed == ["forge/rw:forge/orig"]


def test_resumed_requirements_run_keeps_template_and_opens_no_pr(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _ctx(tmp_path)
    issue = ReadyIssue(9, "t", "b", ["forge:requirements", "bug"], "", "")
    events = [_Evt(EK.RUN_STARTED, "rq", {"issue_number": 9, "trigger": "issue_label",
                                          "focus": "requirements:#9"})]
    seen: dict = {}
    monkeypatch.setattr(bl, "execute_run", lambda **kw: seen.update(kw) or
                        _outcome("no_improvement", None))
    order = ResumeOrder("rq", "s", datetime.now(UTC), 9, "")
    bl._dispatch_resume(ctx=ctx, order=order, params=_params(), issue=issue, events=events)
    assert seen["prompt_template_id"] == "requirements"
    assert seen["create_pr"] is False


def test_failed_push_escalates_instead_of_hanging(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path)
    pr = ctx.code_host.open_change(branch="forge/orig", title="t", body="b", push=False)
    issue = ReadyIssue(8, "t", "b", ["forge:in-dev"], "", "")
    ctx.tracker.add(issue)

    def boom(**kw):
        raise CodeHostError("non-fast-forward")

    monkeypatch.setattr(ctx.code_host, "push_branch", boom)
    monkeypatch.setattr(bl.WorktreeManager, "fetch_remote_branch",
                        lambda self, b, remote="origin": "ref")
    monkeypatch.setattr(bl, "execute_run", lambda **kw: _outcome("pr_created"))
    res = bl._dispatch_branch_run(ctx=ctx, issue=issue, pr_number=pr.pr_number,
                                  params=_params(), kind="rework", context="x")
    assert res.summaries[0].decision == "push_failed"
    assert ctx.tracker.labels_of(8) == ["forge:blocked"]
    store = ctx.open_store()
    (blocked,) = store.events_by_kind(EK.WORK_ITEM_BLOCKED)
    store.close()
    assert "non-fast-forward" in blocked.payload["reason"]


def test_doctor_creates_pr_marker_labels() -> None:
    assert {"forge:auto", "forge:spec", "forge:release-pr"} <= set(MARKER_LABELS)


def test_integration_children_bypass_the_release_train(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx(tmp_path, capabilities=CapabilitiesConfig(create_release=True),
               release=ReleaseConfig(mode="train"))
    ctx.tracker.add(ReadyIssue(11, "child", "Integration-Branch: forge/epic-10",
                               ["forge:release"], "", ""))
    calls: list[int] = []
    monkeypatch.setattr(bl, "_dispatch_release_run",
                        lambda **kw: calls.append(kw["issue"].number) or
                        bl._PassResult([], False, 1, 0))
    seen: dict = {}
    monkeypatch.setattr(bl, "train_tick", lambda ctx, **kw: seen.update(kw) or
                        type("T", (), {"action": "idle", "detail": ""})())
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1)
    assert calls == [11]
    assert seen["waiting_items"] == []


def test_closed_spec_pr_escalates() -> None:
    item = WorkItem(5, Stage.REQUIREMENTS, signals=StageSignals(
        has_refined_spec=True, spec_pending=True, spec_rejected=True))
    plan = plan_tick([item], capacity=1)
    assert plan.transitions[0].to_stage == Stage.BLOCKED
    assert plan.blocked[0].reason == "spec PR closed without merge"


def test_merged_spec_file_wins_over_original_blob(tmp_path: Path) -> None:
    def git(*a):
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a],
                       cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    (tmp_path / "docs" / "specs").mkdir(parents=True)
    (tmp_path / "docs" / "specs" / "7-login.md").write_text("# Spec\n\n1. edited by human\n")
    git("add", ".")
    git("commit", "-q", "-m", "spec")
    ctx = _ctx(tmp_path)
    store = ctx.open_store()
    text = refined_spec_text(store, ctx.open_blobs(), 7, repo_root=tmp_path)
    store.close()
    assert "edited by human" in text


def test_same_titled_children_of_different_epics_are_not_deduplicated(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, capabilities=CapabilitiesConfig(create_work_items=True))
    block = "- id: a\n  kind: task\n  title: Add integration tests\n"
    store = ctx.open_store()
    first = intake_blocks(ctx, store=store, run_id="e1", blocks=[block],
                          source="epic_decomposition", parent=20, parent_approved=True)
    second = intake_blocks(ctx, store=store, run_id="e2", blocks=[block],
                           source="epic_decomposition", parent=30, parent_approved=True)
    store.close()
    assert len(first.created) == 1 and len(second.created) == 1
    assert first.created != second.created
