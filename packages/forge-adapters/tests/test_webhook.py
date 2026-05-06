"""Tests für forge_adapters.github.webhook."""

from __future__ import annotations

from pathlib import Path

import pytest
from forge_adapters.github.webhook import (
    find_run_id_for_pr,
    record_pr_merged,
    record_pr_reverted,
)
from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.store import EventStore

COMMON = dict(
    project="pinta",
    project_fingerprint="sha256:abc",
    factory_version="git:test",
    spec_version="1.0",
)


@pytest.fixture
def store_with_pr(tmp_path: Path):
    store = EventStore(tmp_path / "events.duckdb")
    run_id = "01HZX001"
    store.append_many([
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id=run_id,
            payload=RunStartedPayload(
                trigger="manual", strategy="sequential", config_hash="sha256:cfg"
            ),
            **COMMON,
        ),
        build_event(
            kind=EventKind.PR_CREATED,
            run_id=run_id,
            payload=PRCreatedPayload(pr_number=42, branch=f"forge/{run_id}"),
            **COMMON,
        ),
    ])
    yield store, run_id
    store.close()


def test_find_run_id_for_pr(store_with_pr) -> None:
    store, run_id = store_with_pr
    found = find_run_id_for_pr(store=store, pr_number=42)
    assert found == run_id


def test_find_run_id_returns_none_when_unknown(store_with_pr) -> None:
    store, _ = store_with_pr
    assert find_run_id_for_pr(store=store, pr_number=99999) is None


def test_record_pr_merged(store_with_pr) -> None:
    store, run_id = store_with_pr
    record_pr_merged(
        pr_number=42,
        merger="rudi77",
        time_to_merge_s=3600,
        run_id=run_id,
        store=store,
        **COMMON,
    )
    merged = store.events_by_kind(EventKind.PR_MERGED)
    assert len(merged) == 1
    assert merged[0].payload["pr_number"] == 42
    assert merged[0].payload["merger"] == "rudi77"
    assert merged[0].payload["time_to_merge_s"] == 3600
    assert merged[0].run_id == run_id


def test_record_pr_reverted(store_with_pr) -> None:
    store, run_id = store_with_pr
    record_pr_reverted(
        pr_number=42,
        revert_reason="explicit_revert_commit",
        original_pr=42,
        run_id=run_id,
        store=store,
        **COMMON,
    )
    reverted = store.events_by_kind(EventKind.PR_REVERTED)
    assert len(reverted) == 1
    assert reverted[0].payload["revert_reason"] == "explicit_revert_commit"
    assert reverted[0].payload["original_pr"] == 42
