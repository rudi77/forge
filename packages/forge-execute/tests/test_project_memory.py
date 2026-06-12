"""Tests for cross-run project memory."""

from __future__ import annotations

from pathlib import Path

from forge_core.blobs import BlobStore
from forge_core.events import EventKind, build_event
from forge_core.events.kinds.plan import PlanProposedPayload
from forge_core.events.kinds.pr import PRMergedPayload
from forge_core.events.kinds.run import RunFinishedPayload, RunStartedPayload
from forge_core.store import EventStore
from forge_execute._project_memory import (
    build_project_memory,
    collect_recent_plan_summaries,
    collect_recent_run_outcomes,
    load_memory_seed,
    render_project_memory_addendum,
)

_COMMON = dict(
    project="testproj",
    project_fingerprint="sha256:test",
    factory_version="git:test",
    spec_version="1.0",
)

_SAMPLE_PLAN = """\
# Plan: add render endpoint

## Goal
Expose POST /v1/sessions/{sid}/render.

## Existing patterns I found
- `SegmentEndpoints.cs` groups minimal APIs (src/Ctxman.Api/Endpoints/SegmentEndpoints.cs:12)
- Idempotency via `IdempotencyService` (src/Ctxman.Api/Idempotency/IdempotencyService.cs:8)

## Subtasks
1. **Add IProviderAdapter** — file: `src/Ctxman.Core/Adapters/IProviderAdapter.cs` — change: interface — verified by: unit test
2. **Render endpoint** — file: `src/Ctxman.Api/Endpoints/RenderEndpoints.cs` — change: POST handler — verified by: API test

## Risk
medium
"""


def test_load_memory_seed_reads_forge_memory_md(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "memory.md").write_text("# Stable facts\n\nUse xUnit.\n", encoding="utf-8")
    assert load_memory_seed(forge_dir) == "# Stable facts\n\nUse xUnit."


def test_build_project_memory_from_seed_only(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "memory.md").write_text("Layout: src/ + tests/", encoding="utf-8")
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        md = build_project_memory(
            forge_dir=forge_dir,
            store=store,
            blobs=blobs,
            project="testproj",
        )
        assert "Operator seed" in md
        assert "Layout: src/ + tests/" in md
    finally:
        store.close()


def test_collect_recent_plan_summaries_skips_insufficient_context(
    tmp_path: Path,
) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        good_hash = blobs.put_text(_SAMPLE_PLAN)
        store.append(
            build_event(
                kind=EventKind.PLAN_PROPOSED,
                run_id="run-good",
                generation_id="g1",
                payload=PlanProposedPayload(
                    architect_turns=3,
                    subtask_count=2,
                    insufficient_context=False,
                ),
                artifacts={"plan": good_hash},
                **_COMMON,
            )
        )
        bad_hash = blobs.put_text("# Insufficient context\n\n1. Which library?\n")
        store.append(
            build_event(
                kind=EventKind.PLAN_PROPOSED,
                run_id="run-bad",
                generation_id="g2",
                payload=PlanProposedPayload(insufficient_context=True),
                artifacts={"plan": bad_hash},
                **_COMMON,
            )
        )
        summaries = collect_recent_plan_summaries(
            store, blobs, project="testproj", exclude_run_id=None
        )
        assert len(summaries) == 1
        run_id, summary = summaries[0]
        assert run_id == "run-good"
        assert "add render endpoint" in summary.lower() or "render" in summary.lower()
        assert "IProviderAdapter" in summary or "Subtasks" in summary
    finally:
        store.close()


def test_collect_recent_plan_summaries_excludes_current_run(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        plan_hash = blobs.put_text(_SAMPLE_PLAN)
        store.append(
            build_event(
                kind=EventKind.PLAN_PROPOSED,
                run_id="current-run",
                generation_id="g1",
                payload=PlanProposedPayload(subtask_count=2),
                artifacts={"plan": plan_hash},
                **_COMMON,
            )
        )
        summaries = collect_recent_plan_summaries(
            store,
            blobs,
            project="testproj",
            exclude_run_id="current-run",
        )
        assert summaries == []
    finally:
        store.close()


def _append_run(
    store: EventStore,
    *,
    run_id: str,
    issue: int | None,
    focus: str | None,
    decision: str,
    pr_number: int | None = None,
    merged: bool = False,
) -> None:
    store.append(
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id=run_id,
            payload=RunStartedPayload(
                trigger="manual",
                strategy="sequential",
                config_hash="sha256:" + "0" * 64,
                issue_number=issue,
                focus=focus,
            ),
            **_COMMON,
        )
    )
    store.append(
        build_event(
            kind=EventKind.RUN_FINISHED,
            run_id=run_id,
            payload=RunFinishedPayload(
                decision=decision,  # type: ignore[arg-type]
                generations_count=1,
                pr_number=pr_number,
            ),
            **_COMMON,
        )
    )
    if merged and pr_number is not None:
        store.append(
            build_event(
                kind=EventKind.PR_MERGED,
                run_id=run_id,
                payload=PRMergedPayload(
                    pr_number=pr_number,
                    merger="operator",
                    time_to_merge_s=120,
                ),
                **_COMMON,
            )
        )


def test_collect_recent_run_outcomes_summarizes_runs(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    try:
        _append_run(
            store,
            run_id="run-wp4",
            issue=12,
            focus="WP4: render endpoint",
            decision="pr_created",
            pr_number=34,
            merged=True,
        )
        _append_run(
            store,
            run_id="run-fail",
            issue=13,
            focus=None,
            decision="no_improvement",
        )
        lines = collect_recent_run_outcomes(store, project="testproj")
        assert len(lines) == 2
        joined = "\n".join(lines)
        assert "issue #12" in joined
        assert "pr_created" in joined
        assert "PR #34, merged" in joined
        assert "WP4: render endpoint" in joined
        assert "no_improvement" in joined
    finally:
        store.close()


def test_collect_recent_run_outcomes_excludes_current_run(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    try:
        _append_run(
            store,
            run_id="current-run",
            issue=1,
            focus=None,
            decision="pr_created",
        )
        assert (
            collect_recent_run_outcomes(
                store, project="testproj", exclude_run_id="current-run"
            )
            == []
        )
    finally:
        store.close()


def test_build_project_memory_includes_run_outcomes(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    # Bewusst veralteter Seed: der Event-Strom muss den Erledigt-Status
    # liefern, auch wenn die Operator-Prosa nicht nachgepflegt wurde.
    (forge_dir / "memory.md").write_text(
        "## Noch offen\n- WP4 render endpoint", encoding="utf-8"
    )
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        _append_run(
            store,
            run_id="run-wp4",
            issue=12,
            focus="WP4: render endpoint",
            decision="pr_created",
            pr_number=34,
            merged=True,
        )
        md = build_project_memory(
            forge_dir=forge_dir,
            store=store,
            blobs=blobs,
            project="testproj",
        )
        assert "Recent run outcomes" in md
        assert "issue #12" in md
        assert "do not re-plan" in md
    finally:
        store.close()


def test_render_project_memory_addendum_empty_when_no_memory() -> None:
    assert render_project_memory_addendum("") == ""
    assert render_project_memory_addendum("   ") == ""


def test_render_project_memory_addendum_wraps_content() -> None:
    wrapped = render_project_memory_addendum("## Operator seed\nFacts here.")
    assert "## forge project memory" in wrapped
    assert "Facts here." in wrapped
    assert "task-specific Spec sections" in wrapped


def test_build_project_memory_is_deterministic(tmp_path: Path) -> None:
    forge_dir = tmp_path / ".forge"
    forge_dir.mkdir()
    (forge_dir / "memory.md").write_text("seed", encoding="utf-8")
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        plan_hash = blobs.put_text(_SAMPLE_PLAN)
        store.append(
            build_event(
                kind=EventKind.PLAN_PROPOSED,
                run_id="run-1",
                generation_id="g1",
                payload=PlanProposedPayload(subtask_count=2),
                artifacts={"plan": plan_hash},
                **_COMMON,
            )
        )
        first = build_project_memory(
            forge_dir=forge_dir,
            store=store,
            blobs=blobs,
            project="testproj",
        )
        second = build_project_memory(
            forge_dir=forge_dir,
            store=store,
            blobs=blobs,
            project="testproj",
        )
        assert first == second
        assert "Recent successful plans" in first
    finally:
        store.close()
