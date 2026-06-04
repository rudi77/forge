"""Tests für den Watch-Modus (Conductor Phase B) im board-loop.

Wir testen die Watch-Glue ``_run_watch`` mit gestubbtem Board + gestubbtem
Dispatch — die Heartbeat-Engine (``run_heartbeat``) und der Cron-Matcher sind
separat voll unit-getestet, ``_dispatch_issues`` ist der verhaltensgleiche
Ex-Inline-Loop. Hier interessiert nur: Ticks laufen, Events werden persistiert.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

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


def _issue(n: int) -> ReadyIssue:
    return ReadyIssue(
        number=n,
        title=f"issue {n}",
        body="body",
        labels=["bug"],
        project_status="Ready",
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


def test_watch_runs_bounded_ticks_and_emits_events(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    monkeypatch.setattr(bl, "list_ready_items", lambda *a, **k: [_issue(1)])
    # Dispatch stubben: ein Item dispatcht, kein bail.
    monkeypatch.setattr(
        bl,
        "_dispatch_issues",
        lambda **k: bl._PassResult(
            summaries=[], bailed=False, dispatched=1, skipped=0
        ),
    )

    stats = bl._run_watch(
        ctx=ctx,
        repo_owner="x",
        repo_name="y",
        max_issues=3,
        interval_s=0,
        params=_params(),
        triager=None,
        capabilities=None,
        max_ticks=2,
    )

    assert stats.ticks == 2
    assert stats.total_dispatched == 2
    assert stats.stopped_reason == "max_ticks"

    store = ctx.open_store()
    try:
        rows = store.query(
            "SELECT * FROM events WHERE kind = 'ConductorTickCompleted' "
            "ORDER BY ts"
        )
    finally:
        store.close()
    assert len(rows) == 2


def test_watch_empty_backlog_emits_idle_ticks(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    monkeypatch.setattr(bl, "list_ready_items", lambda *a, **k: [])

    stats = bl._run_watch(
        ctx=ctx,
        repo_owner="x",
        repo_name="y",
        max_issues=3,
        interval_s=0,
        params=_params(),
        triager=None,
        capabilities=None,
        max_ticks=2,
    )
    # Leerer Backlog → Ticks laufen, aber nichts wird dispatcht.
    assert stats.ticks == 2
    assert stats.total_dispatched == 0

    store = ctx.open_store()
    try:
        rows = store.query(
            "SELECT * FROM events WHERE kind = 'ConductorTickCompleted'"
        )
    finally:
        store.close()
    assert len(rows) == 2


def test_watch_board_error_does_not_crash_tick(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)

    def _boom(*_a: Any, **_k: Any) -> list[ReadyIssue]:
        raise bl.BoardError("gh exploded")

    monkeypatch.setattr(bl, "list_ready_items", _boom)

    # Board-Fehler pro Tick wird gefangen — der Heartbeat läuft weiter.
    stats = bl._run_watch(
        ctx=ctx,
        repo_owner="x",
        repo_name="y",
        max_issues=3,
        interval_s=0,
        params=_params(),
        triager=None,
        capabilities=None,
        max_ticks=2,
    )
    assert stats.ticks == 2
    assert stats.total_dispatched == 0
