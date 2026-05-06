"""Tests für forge_core.store."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.events import (
    EvalFinishedPayload,
    EventKind,
    PRCreatedPayload,
    PRMergedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.events.kinds.eval_ import GateResult
from forge_core.store import EventStore

COMMON = dict(
    project="pinta",
    project_fingerprint="sha256:7a3c",
    factory_version="git:abc",
    spec_version="1.0",
)


@pytest.fixture
def store(tmp_path: Path):
    s = EventStore(tmp_path / "events.duckdb")
    yield s
    s.close()


def _seed_minimal_run(store: EventStore, run_id: str = "r1", focus: str = "legacy_test_revival") -> int:
    """Schreibt einen kompletten Run und liefert den emulierten PR-Number."""
    pr_number = 42
    events = [
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id=run_id,
            payload=RunStartedPayload(
                trigger="manual",
                strategy="sequential",
                config_hash="sha256:cfg",
                focus=focus,
            ),
            **COMMON,
        ),
        build_event(
            kind=EventKind.PR_CREATED,
            run_id=run_id,
            payload=PRCreatedPayload(pr_number=pr_number, branch=f"forge/{run_id}"),
            artifacts={"diff": "sha256:" + "a" * 64},
            **COMMON,
        ),
        build_event(
            kind=EventKind.RUN_FINISHED,
            run_id=run_id,
            payload=RunFinishedPayload(
                decision="pr_created",
                generations_count=3,
                final_score=0.81,
                score_delta=0.04,
                total_cost_usd=Decimal("0.94"),
                pr_number=pr_number,
            ),
            **COMMON,
        ),
    ]
    store.append_many(events)
    return pr_number


def test_append_and_count(store: EventStore) -> None:
    _seed_minimal_run(store)
    assert store.count() == 3


def test_append_idempotent_on_event_id(store: EventStore) -> None:
    evt = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="r1",
        payload=RunStartedPayload(
            trigger="manual", strategy="sequential", config_hash="sha256:cfg"
        ),
        **COMMON,
    )
    store.append(evt)
    store.append(evt)
    assert store.count() == 1


def test_events_for_run_roundtrip_preserves_payload(store: EventStore) -> None:
    eval_payload = EvalFinishedPayload(
        eval_mode="quick",
        suite_id="quick",
        gates_passed=True,
        gates=[GateResult(kind="pytest_pass_rate", passed=True, actual=1.0, threshold=1.0)],
        scores={"coverage_pct": 87.2, "p95_latency_ms": 412.0},
        composite_value=0.71,
        diagnostics={"llm_judge_score": 0.82},
    )
    evt = build_event(
        kind=EventKind.EVAL_FINISHED,
        run_id="r1",
        generation_id="g1",
        payload=eval_payload,
        artifacts={"eval_stdout": "sha256:" + "b" * 64},
        **COMMON,
    )
    store.append(evt)
    [back] = store.events_for_run("r1")
    assert back.payload["gates_passed"] is True
    assert back.payload["scores"]["coverage_pct"] == 87.2
    assert back.payload["composite_value"] == 0.71
    assert back.artifacts["eval_stdout"].startswith("sha256:")


def test_runs_with_outcomes_view(store: EventStore) -> None:
    _seed_minimal_run(store)
    rows = store.query("SELECT * FROM runs_with_outcomes")
    assert len(rows) == 1
    assert rows[0]["decision"] == "pr_created"
    assert rows[0]["focus"] == "legacy_test_revival"
    assert rows[0]["final_score"] == pytest.approx(0.81)


def test_pr_merge_rate_view(store: EventStore) -> None:
    pr = _seed_minimal_run(store, run_id="r1")
    # Merge-Event von außen, ohne run_id-Verbindung — Verknüpfung läuft über pr_number
    merge_evt = build_event(
        kind=EventKind.PR_MERGED,
        run_id="r1",
        payload=PRMergedPayload(pr_number=pr, merger="rudi", time_to_merge_s=3600),
        **COMMON,
    )
    store.append(merge_evt)

    rows = store.query("SELECT * FROM pr_merge_rate_by_focus")
    assert len(rows) == 1
    assert rows[0]["prs_created"] == 1
    assert rows[0]["prs_merged"] == 1
    assert rows[0]["merge_rate"] == pytest.approx(1.0)


def test_referenced_artifact_hashes(store: EventStore) -> None:
    _seed_minimal_run(store)
    refs = store.referenced_artifact_hashes()
    assert "sha256:" + "a" * 64 in refs


def test_query_returns_dicts(store: EventStore) -> None:
    _seed_minimal_run(store)
    rows = store.query("SELECT kind, COUNT(*) AS n FROM events GROUP BY kind")
    by_kind = {r["kind"]: r["n"] for r in rows}
    assert by_kind["RunStarted"] == 1
    assert by_kind["RunFinished"] == 1


def test_context_manager_closes(tmp_path: Path) -> None:
    db = tmp_path / "events.duckdb"
    with EventStore(db) as s:
        assert s.count() == 0
    # Nach close() ist die DB-Datei freigegeben (Windows: kein Lock)
    assert db.exists()
