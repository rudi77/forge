"""Tests für den Schedule-Trigger-Dispatch im Heartbeat
(``_dispatch_due_schedules``): Prompt-Quelle ist die ``prompt_file``, der
``last``-Anker kommt aus dem Event-Strom (jüngstes ``RunStarted`` mit
``trigger=schedule`` + Focus)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_cli import board_loop as bl
from forge_cli.runtime import ForgeContext
from forge_core.events import EventKind, RunStartedPayload, build_event
from forge_core.spec import (
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    ScheduleTriggerConfig,
    TriageConfig,
    TriggersConfig,
)


def _make_ctx(tmp_path: Path, *, prompt_file: str | None) -> ForgeContext:
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
        triage=TriageConfig(enabled=False),
        triggers=TriggersConfig(
            schedule=[
                ScheduleTriggerConfig(
                    cron="* * * * *",
                    focus="nightly_sweep",
                    prompt_file=prompt_file,
                )
            ]
        ),
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


def _outcome(decision: str = "pr_created") -> Any:
    from forge_execute.runner import RunResult

    return bl.RunOutcome(result=RunResult(run_id="r1", decision=decision))


def _seed_run_started(
    store: Any, *, focus: str, ts: datetime, trigger: str = "schedule"
) -> None:
    store.append(
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r-prev",
            project="testproj",
            project_fingerprint="sha256:test",
            factory_version="git:test",
            spec_version="1.0",
            payload=RunStartedPayload(
                trigger=trigger,
                strategy="sequential",
                config_hash="sha256:cfg",
                focus=focus,
            ),
            ts=ts,
        )
    )


def test_due_schedule_fires_and_passes_prompt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt_path = tmp_path / ".forge" / "prompts" / "nightly.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("Räume tote Tests auf.", encoding="utf-8")
    ctx = _make_ctx(tmp_path, prompt_file=".forge/prompts/nightly.md")

    calls: list[dict[str, Any]] = []

    def fake_execute_run(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return _outcome()

    monkeypatch.setattr(bl, "execute_run", fake_execute_run)
    store = ctx.open_store()
    try:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        res = bl._dispatch_due_schedules(
            ctx=ctx,
            store=store,
            params=_params(),
            session_start=now - timedelta(minutes=5),
            now=now,
        )
    finally:
        store.close()

    assert res.scheduled == 1
    assert not res.bailed
    assert len(calls) == 1
    assert calls[0]["trigger"] == "schedule"
    assert calls[0]["focus"] == "nightly_sweep"
    assert calls[0]["rendered_prompt"] == "Räume tote Tests auf."
    assert calls[0]["agents"] == ["architect", "developer", "tester"]
    assert calls[0]["create_pr"] is True


def test_schedule_without_prompt_file_is_skipped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path, prompt_file=None)
    monkeypatch.setattr(
        bl,
        "execute_run",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    store = ctx.open_store()
    try:
        now = datetime.now(UTC)
        res = bl._dispatch_due_schedules(
            ctx=ctx,
            store=store,
            params=_params(),
            session_start=now - timedelta(minutes=5),
            now=now,
        )
    finally:
        store.close()
    assert res.scheduled == 0


def test_schedule_with_missing_prompt_file_is_skipped(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path, prompt_file=".forge/prompts/missing.md")
    monkeypatch.setattr(
        bl,
        "execute_run",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not dispatch")),
    )
    store = ctx.open_store()
    try:
        now = datetime.now(UTC)
        res = bl._dispatch_due_schedules(
            ctx=ctx,
            store=store,
            params=_params(),
            session_start=now - timedelta(minutes=5),
            now=now,
        )
    finally:
        store.close()
    assert res.scheduled == 0


def test_last_run_from_event_stream_suppresses_refire(
    tmp_path: Path, monkeypatch: Any
) -> None:
    prompt_path = tmp_path / ".forge" / "prompts" / "nightly.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("auftrag", encoding="utf-8")
    ctx = _make_ctx(tmp_path, prompt_file=".forge/prompts/nightly.md")
    monkeypatch.setattr(
        bl,
        "execute_run",
        lambda **k: (_ for _ in ()).throw(AssertionError("must not refire")),
    )
    store = ctx.open_store()
    try:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        # Der Trigger lief in genau dieser Minute schon → nicht erneut fällig.
        _seed_run_started(store, focus="nightly_sweep", ts=now)
        res = bl._dispatch_due_schedules(
            ctx=ctx,
            store=store,
            params=_params(),
            session_start=now - timedelta(hours=1),
            now=now,
        )
    finally:
        store.close()
    assert res.scheduled == 0


def test_bad_run_decision_bails(tmp_path: Path, monkeypatch: Any) -> None:
    prompt_path = tmp_path / ".forge" / "prompts" / "nightly.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("auftrag", encoding="utf-8")
    ctx = _make_ctx(tmp_path, prompt_file=".forge/prompts/nightly.md")
    monkeypatch.setattr(bl, "execute_run", lambda **k: _outcome("cost_cap_hit"))
    store = ctx.open_store()
    try:
        now = datetime.now(UTC).replace(second=0, microsecond=0)
        res = bl._dispatch_due_schedules(
            ctx=ctx,
            store=store,
            params=_params(),
            session_start=now - timedelta(minutes=5),
            now=now,
        )
    finally:
        store.close()
    assert res.scheduled == 1
    assert res.bailed


def test_watch_tick_counts_scheduled_runs(tmp_path: Path, monkeypatch: Any) -> None:
    """Integration: _run_watch nimmt den Schedule-Zähler in die Tick-Stats auf."""
    from forge_core.spec import BoardConfig

    prompt_path = tmp_path / ".forge" / "prompts" / "nightly.md"
    prompt_path.parent.mkdir(parents=True)
    prompt_path.write_text("auftrag", encoding="utf-8")
    ctx = _make_ctx(tmp_path, prompt_file=".forge/prompts/nightly.md")
    object.__setattr__(ctx.spec, "board", BoardConfig(owner="x", project_number=1))

    fired: list[str] = []

    def fake_dispatch_due_schedules(**kwargs: Any) -> bl._ScheduleResult:
        fired.append("tick")
        return bl._ScheduleResult(scheduled=1)

    monkeypatch.setattr(bl, "_dispatch_due_schedules", fake_dispatch_due_schedules)
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
    assert stats.ticks == 2
    assert stats.total_scheduled == 2
    assert len(fired) == 2
