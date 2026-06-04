"""Tests für `forge analyze` — Report-Rendering, inkl. Fabrik-Sektionen."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from forge_cli.analyze import _fmt_duration, _render_report
from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    PRMergedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.store import EventStore

_COMMON = dict(
    project="pinta",
    project_fingerprint="sha256:7a3c",
    factory_version="git:abc",
    spec_version="1.0",
)


def test_render_report_empty_store_has_factory_sections(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    try:
        md = _render_report(store, project="pinta", last_runs=5)
    finally:
        store.close()
    assert "## Factory KPIs" in md
    assert "## Throughput (per day)" in md
    # Leerer Store → klarer „no data"-Hinweis, kein Crash.
    assert "_no runs yet._" in md


def test_render_report_with_run_shows_kpis(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    try:
        store.append_many(
            [
                build_event(
                    kind=EventKind.RUN_STARTED,
                    run_id="r1",
                    payload=RunStartedPayload(
                        trigger="manual",
                        strategy="sequential",
                        config_hash="sha256:cfg",
                        focus="legacy_test_revival",
                    ),
                    **_COMMON,
                ),
                build_event(
                    kind=EventKind.PR_CREATED,
                    run_id="r1",
                    payload=PRCreatedPayload(pr_number=7, branch="forge/r1"),
                    **_COMMON,
                ),
                build_event(
                    kind=EventKind.PR_MERGED,
                    run_id="r1",
                    payload=PRMergedPayload(
                        pr_number=7, merger="rudi", time_to_merge_s=7200
                    ),
                    **_COMMON,
                ),
                build_event(
                    kind=EventKind.RUN_FINISHED,
                    run_id="r1",
                    payload=RunFinishedPayload(
                        decision="pr_created",
                        generations_count=1,
                        final_score=0.8,
                        total_cost_usd=Decimal("1.50"),
                        pr_number=7,
                    ),
                    **_COMMON,
                ),
            ]
        )
        md = _render_report(store, project="pinta", last_runs=5)
    finally:
        store.close()

    assert "| Runs total | 1 |" in md
    assert "| PRs merged | 1 |" in md
    # Lead-Time 7200s → 2.0h (siehe _fmt_duration).
    assert "2.0h" in md


def test_fmt_duration_buckets() -> None:
    assert _fmt_duration(None) == "—"
    assert _fmt_duration(45) == "45s"
    assert _fmt_duration(120) == "2m"
    assert _fmt_duration(7200) == "2.0h"
    assert _fmt_duration(172800) == "2.0d"
