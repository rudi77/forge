"""Tests für den Conductor-Watch-Modus (Phase C Integration) im board-loop.

Board (list_stage_items / set_issue_stage_label) und Dispatch (_dispatch_issues)
werden gestubbt — die State-Machine + plan_tick sind separat unit-getestet.
Hier interessiert die Verdrahtung: werden Übergänge effektiert, wird dispatcht,
landen WorkItemStageChanged/WorkItemBlocked + ConductorTickCompleted im Store?
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from forge_adapters.github import ReadyIssue
from forge_cli import board_loop as bl
from forge_cli.runtime import ForgeContext
from forge_core.spec import (
    BoardConfig,
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    TriageConfig,
)


def _make_ctx(tmp_path: Path) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0",
        name="testproj",
        cost_caps=CostCapsConfig(
            per_generation_usd=Decimal("0.5"),
            per_run_usd=Decimal("2.0"),
            per_project_per_day_usd=Decimal("10.0"),
            per_project_per_month_usd=Decimal("50.0"),
        ),
        capabilities=CapabilitiesConfig(),
        board=BoardConfig(owner="x", project_number=1),
        triage=TriageConfig(enabled=False),
    )
    return ForgeContext(
        repo_root=tmp_path,
        forge_dir=tmp_path / ".forge",
        spec=spec,
        spec_path=tmp_path / ".forge" / "project.yaml",
        factory_version="git:test",
        project_fingerprint="sha256:test",
        store_path=tmp_path / "events.duckdb",
        blobs_path=tmp_path / "blobs",
    )


def _issue(n: int, labels: list[str], body: str = "") -> ReadyIssue:
    return ReadyIssue(
        number=n,
        title=f"issue {n}",
        body=body,
        labels=labels,
        project_status="",
        url=f"https://github.com/x/y/issues/{n}",
    )


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t",
        focus_template="issue-{number}",
        base_ref="HEAD",
        max_iterations=1,
        max_turns=4,
        eval_suite="quick",
        model=None,
        claude_bin="claude",
        multi_agent=False,
        auto_merge=False,
        pr_base="main",
        pr_label=None,
    )


def _run(ctx: ForgeContext, max_ticks: int = 1) -> bl.HeartbeatStats:
    return bl._run_conductor_watch(
        ctx=ctx,
        repo_owner="x",
        repo_name="y",
        max_issues=3,
        interval_s=0,
        params=_params(),
        triager=None,
        capabilities=None,
        max_ticks=max_ticks,
    )


def _events_of_kind(ctx: ForgeContext, kind: str) -> int:
    store = ctx.open_store()
    try:
        rows = store.query(
            "SELECT count(*) AS n FROM events WHERE kind = ?", [kind]
        )
    finally:
        store.close()
    return rows[0]["n"]


def test_conductor_dispatches_ready_item_and_records_transition(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    monkeypatch.setattr(
        bl, "list_stage_items", lambda **k: [_issue(5, ["forge:ready"])]
    )
    set_label = MagicMock()
    monkeypatch.setattr(bl, "set_issue_stage_label", set_label)
    dispatched: list[int] = []

    def fake_dispatch(*, ctx, issues, params, triager, capabilities, store=None):
        dispatched.extend(i.number for i in issues)
        return bl._PassResult(
            summaries=[], bailed=False, dispatched=len(issues), skipped=0
        )

    monkeypatch.setattr(bl, "_dispatch_issues", fake_dispatch)

    stats = _run(ctx)
    assert stats.ticks == 1
    assert stats.total_dispatched == 1
    assert dispatched == [5]
    # ready→in-dev wurde via gh effektiert.
    _, kwargs = set_label.call_args
    assert kwargs["add"] == "forge:in-dev"
    assert kwargs["remove"] == "forge:ready"
    # Events: ein Stage-Übergang + ein Tick.
    assert _events_of_kind(ctx, "WorkItemStageChanged") == 1
    assert _events_of_kind(ctx, "ConductorTickCompleted") == 1


def test_conductor_blocks_item_with_unmet_dependency(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    # #2 (ready) hängt von #1 ab, das nirgends als done sichtbar ist → blocked.
    monkeypatch.setattr(
        bl,
        "list_stage_items",
        lambda **k: [_issue(2, ["forge:ready"], body="Depends-On: #1")],
    )
    monkeypatch.setattr(bl, "set_issue_stage_label", MagicMock())
    dispatched: list[int] = []
    monkeypatch.setattr(
        bl,
        "_dispatch_issues",
        lambda **k: dispatched.append(1)
        or bl._PassResult(summaries=[], bailed=False, dispatched=1, skipped=0),
    )

    stats = _run(ctx)
    assert stats.total_dispatched == 0
    assert dispatched == []
    assert _events_of_kind(ctx, "WorkItemBlocked") == 1


def test_conductor_dispatches_when_dependency_done(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    # #1 done (sichtbar via state=all), #2 ready depends on #1 → dispatch #2.
    monkeypatch.setattr(
        bl,
        "list_stage_items",
        lambda **k: [
            _issue(1, ["forge:done"]),
            _issue(2, ["forge:ready"], body="Depends-On: #1"),
        ],
    )
    monkeypatch.setattr(bl, "set_issue_stage_label", MagicMock())
    dispatched: list[int] = []

    def fake_dispatch(*, ctx, issues, params, triager, capabilities, store=None):
        dispatched.extend(i.number for i in issues)
        return bl._PassResult(
            summaries=[], bailed=False, dispatched=len(issues), skipped=0
        )

    monkeypatch.setattr(bl, "_dispatch_issues", fake_dispatch)

    _run(ctx)
    assert dispatched == [2]


def test_conductor_dispatches_design_stage_to_design_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    # Ein design-Item ohne Plan → der Design-Run (architect-Team) läuft,
    # NICHT der Dev-Loop. Kein Stage-Wechsel (bleibt design bis Plan vorliegt).
    monkeypatch.setattr(
        bl, "list_stage_items", lambda **k: [_issue(9, ["forge:design"])]
    )
    set_label = MagicMock()
    monkeypatch.setattr(bl, "set_issue_stage_label", set_label)

    design_runs: list[int] = []

    def fake_design(*, ctx, issue, params, store=None):
        design_runs.append(issue.number)
        return bl._PassResult(
            summaries=[], bailed=False, dispatched=1, skipped=0
        )

    # Der Dev-Loop darf hier NICHT angefasst werden.
    dev_runs: list[int] = []
    monkeypatch.setattr(bl, "_dispatch_design_run", fake_design)
    monkeypatch.setattr(
        bl,
        "_dispatch_issues",
        lambda **k: dev_runs.append(1)
        or bl._PassResult(summaries=[], bailed=False, dispatched=1, skipped=0),
    )

    stats = _run(ctx)
    assert design_runs == [9]
    assert dev_runs == []  # kein Dev-Loop für ein design-Item
    assert stats.total_dispatched == 1
    # design bleibt design — kein gh-Label-Wechsel in diesem Tick.
    set_label.assert_not_called()
    assert _events_of_kind(ctx, "WorkItemStageChanged") == 0


def test_conductor_empty_board_emits_tick_only(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    monkeypatch.setattr(bl, "list_stage_items", lambda **k: [])
    monkeypatch.setattr(bl, "set_issue_stage_label", MagicMock())

    stats = _run(ctx, max_ticks=2)
    assert stats.total_dispatched == 0
    assert _events_of_kind(ctx, "ConductorTickCompleted") == 2
    assert _events_of_kind(ctx, "WorkItemStageChanged") == 0


def test_conductor_parallel_dispatches_two_ready_items(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # max_parallel=2: zwei ready-Items werden in EINEM Tick nebenläufig dispatcht.
    import threading

    ctx = _make_ctx(tmp_path)
    monkeypatch.setattr(
        bl,
        "list_stage_items",
        lambda **k: [_issue(5, ["forge:ready"]), _issue(6, ["forge:ready"])],
    )
    monkeypatch.setattr(bl, "set_issue_stage_label", MagicMock())

    lock = threading.Lock()
    dispatched: list[int] = []

    def fake_dispatch(*, ctx, issues, params, triager, capabilities, store=None):
        with lock:
            dispatched.extend(i.number for i in issues)
        return bl._PassResult(
            summaries=[], bailed=False, dispatched=len(issues), skipped=0
        )

    monkeypatch.setattr(bl, "_dispatch_issues", fake_dispatch)

    stats = bl._run_conductor_watch(
        ctx=ctx, repo_owner="x", repo_name="y", max_issues=3, interval_s=0,
        params=_params(), triager=None, capabilities=None, max_ticks=1,
        max_parallel=2,
    )
    assert stats.total_dispatched == 2
    assert sorted(dispatched) == [5, 6]


def test_conductor_qa_stage_dispatches_review_run(
    tmp_path: Path, monkeypatch: Any
) -> None:
    # Ein qa-Item mit offenem PR → der Review-Merge-Run läuft mit der via
    # Event-Strom aufgelösten PR-Nummer; kein Dev-Loop.
    from forge_core.events import (
        EventKind,
        PRCreatedPayload,
        RunStartedPayload,
        build_event,
    )

    ctx = _make_ctx(tmp_path)
    # Event-Strom seeden: Run für Issue #8 hat PR #42 erzeugt.
    store = ctx.open_store()
    common = dict(
        project="testproj", project_fingerprint="sha256:test",
        factory_version="git:test", spec_version="1.0",
    )
    store.append(build_event(
        kind=EventKind.RUN_STARTED, run_id="r8",
        payload=RunStartedPayload(
            trigger="issue_label", strategy="sequential", config_hash="c",
            issue_number=8,
        ),
        **common,
    ))
    store.append(build_event(
        kind=EventKind.PR_CREATED, run_id="r8",
        payload=PRCreatedPayload(pr_number=42, branch="forge/r8"),
        **common,
    ))
    store.close()

    monkeypatch.setattr(
        bl, "list_stage_items", lambda **k: [_issue(8, ["forge:qa"])]
    )
    monkeypatch.setattr(bl, "set_issue_stage_label", MagicMock())

    reviews: list[int] = []

    def fake_review(*, ctx, issue, pr_number, params, store=None):
        reviews.append(pr_number)
        return bl._PassResult(
            summaries=[], bailed=False, dispatched=1, skipped=0
        )

    dev_runs: list[int] = []
    monkeypatch.setattr(bl, "_dispatch_review_run", fake_review)
    monkeypatch.setattr(
        bl, "_dispatch_issues",
        lambda **k: dev_runs.append(1)
        or bl._PassResult(summaries=[], bailed=False, dispatched=1, skipped=0),
    )

    stats = _run(ctx)
    assert reviews == [42]  # PR-Nummer via pr_number_for_issue aufgelöst
    assert dev_runs == []
    assert stats.total_dispatched == 1
