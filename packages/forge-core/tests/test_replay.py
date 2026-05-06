"""Tests für forge_core.replay."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.blobs import BlobStore
from forge_core.events import (
    EventKind,
    GenerationFinishedPayload,
    GenerationStartedPayload,
    ProposalReceivedPayload,
    ProposalRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.replay import reconstruct_run
from forge_core.store import EventStore

COMMON = dict(
    project="pinta",
    project_fingerprint="sha256:7a3c",
    factory_version="git:abc",
    spec_version="1.0",
)


@pytest.fixture
def store_and_blobs(tmp_path: Path):
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    yield store, blobs
    store.close()


def _seed_run(
    store: EventStore,
    blobs: BlobStore,
    *,
    run_id: str = "r1",
    gen_id: str = "g1",
) -> tuple[str, str]:
    prompt_hash = blobs.put_text("Du bist forge. Fixe den Test.")
    diff_hash = blobs.put_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n")

    events = [
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id=run_id,
            payload=RunStartedPayload(
                trigger="manual",
                strategy="sequential",
                config_hash="sha256:cfg",
            ),
            **COMMON,
        ),
        build_event(
            kind=EventKind.GENERATION_STARTED,
            run_id=run_id,
            generation_id=gen_id,
            payload=GenerationStartedPayload(generation_idx=0),
            **COMMON,
        ),
        build_event(
            kind=EventKind.PROPOSAL_REQUESTED,
            run_id=run_id,
            generation_id=gen_id,
            payload=ProposalRequestedPayload(prompt_template_id="t1"),
            artifacts={"prompt": prompt_hash},
            **COMMON,
        ),
        build_event(
            kind=EventKind.PROPOSAL_RECEIVED,
            run_id=run_id,
            generation_id=gen_id,
            payload=ProposalReceivedPayload(stop_reason="end_turn", turns_used=2),
            artifacts={"diff": diff_hash},
            cost_usd=Decimal("0.018"),
            **COMMON,
        ),
        build_event(
            kind=EventKind.GENERATION_FINISHED,
            run_id=run_id,
            generation_id=gen_id,
            payload=GenerationFinishedPayload(
                generation_idx=0,
                decision="keep",
                new_score=0.71,
                score_delta=0.04,
                reason="improvement",
            ),
            **COMMON,
        ),
        build_event(
            kind=EventKind.RUN_FINISHED,
            run_id=run_id,
            payload=RunFinishedPayload(
                decision="pr_created",
                generations_count=1,
                final_score=0.71,
            ),
            **COMMON,
        ),
    ]
    store.append_many(events)
    return prompt_hash, diff_hash


def test_reconstruct_resolves_prompt_and_diff(store_and_blobs) -> None:
    store, blobs = store_and_blobs
    _seed_run(store, blobs)

    rec = reconstruct_run("r1", store=store, blobs=blobs)
    assert rec.project == "pinta"
    assert rec.factory_version == "git:abc"
    assert len(rec.events) == 6
    assert len(rec.generations) == 1

    g = rec.generations[0]
    assert g.proposal_prompt is not None and "Fixe den Test" in g.proposal_prompt
    assert g.diff is not None and "+++ b/x" in g.diff


def test_reconstruct_without_blobstore_returns_events_only(store_and_blobs) -> None:
    store, blobs = store_and_blobs
    _seed_run(store, blobs)

    rec = reconstruct_run("r1", store=store, blobs=None)
    assert len(rec.events) == 6
    g = rec.generations[0]
    assert g.artifacts == {}
    assert g.proposal_prompt is None


def test_reconstruct_skips_missing_blobs(store_and_blobs, tmp_path: Path) -> None:
    store, blobs = store_and_blobs
    _seed_run(store, blobs)
    # Leeres Blob-Store als Stand-in für „Blobs verloren"
    empty_blobs = BlobStore(tmp_path / "empty-blobs")

    rec = reconstruct_run("r1", store=store, blobs=empty_blobs)
    g = rec.generations[0]
    # Keine Artefakte aufgelöst, aber Events vorhanden — Replay ist „unvollständig", nicht „kaputt"
    assert g.artifacts == {}
    assert len(g.events) > 0


def test_reconstruct_started_finished_at(store_and_blobs) -> None:
    store, blobs = store_and_blobs
    _seed_run(store, blobs)
    rec = reconstruct_run("r1", store=store, blobs=blobs)
    assert rec.started_at is not None
    assert rec.finished_at is not None
    assert rec.started_at <= rec.finished_at
