"""End-to-End-Test: 5 Events anlegen, queryen, reconstruct'en.

Erfolgskriterium aus Schritt 2 der todos: ein Test, der genau das macht.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from forge_core.blobs import BlobStore
from forge_core.events import (
    EventKind,
    PRCreatedPayload,
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


def test_end_to_end_5_events_query_and_reconstruct(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    prompt_hash = blobs.put_text("fix the failing test")
    diff_hash = blobs.put_text("diff content")

    events = [
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="run-1",
            payload=RunStartedPayload(
                trigger="manual",
                strategy="sequential",
                config_hash="sha256:cfg",
                focus="legacy_test_revival",
            ),
            **COMMON,
        ),
        build_event(
            kind=EventKind.PROPOSAL_REQUESTED,
            run_id="run-1",
            generation_id="gen-1",
            payload=ProposalRequestedPayload(prompt_template_id="fix_failing_test_v3"),
            artifacts={"prompt": prompt_hash},
            tokens_in=4521,
            **COMMON,
        ),
        build_event(
            kind=EventKind.PROPOSAL_RECEIVED,
            run_id="run-1",
            generation_id="gen-1",
            payload=ProposalReceivedPayload(stop_reason="end_turn", turns_used=3),
            artifacts={"diff": diff_hash},
            tokens_out=612,
            cost_usd=Decimal("0.018"),
            **COMMON,
        ),
        build_event(
            kind=EventKind.PR_CREATED,
            run_id="run-1",
            payload=PRCreatedPayload(pr_number=42, branch="forge/run-1"),
            **COMMON,
        ),
        build_event(
            kind=EventKind.RUN_FINISHED,
            run_id="run-1",
            payload=RunFinishedPayload(
                decision="pr_created",
                generations_count=1,
                final_score=0.81,
                total_cost_usd=Decimal("0.018"),
                pr_number=42,
            ),
            **COMMON,
        ),
    ]
    store.append_many(events)

    # 1) Query
    assert store.count() == 5
    [run] = store.query("SELECT * FROM runs_with_outcomes")
    assert run["decision"] == "pr_created"
    assert run["focus"] == "legacy_test_revival"

    # 2) Reconstruct
    rec = reconstruct_run("run-1", store=store, blobs=blobs)
    assert len(rec.events) == 5
    assert len(rec.generations) == 1
    g = rec.generations[0]
    assert g.proposal_prompt == "fix the failing test"
    assert g.diff == "diff content"

    # 3) GC respektiert referenzierte Hashes
    refs = store.referenced_artifact_hashes()
    assert prompt_hash in refs
    assert diff_hash in refs

    store.close()
