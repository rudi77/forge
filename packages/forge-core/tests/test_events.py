"""Tests für forge_core.events."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from forge_core.events import (
    EventKind,
    RunFinishedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.events.base import _PAYLOAD_REGISTRY

COMMON = dict(
    project="pinta",
    project_fingerprint="sha256:7a3c",
    factory_version="git:abc",
    spec_version="1.0",
)


def test_all_event_kinds_have_registered_payload_schemas() -> None:
    # Spec v0.4: 17 (v0.3) + IssueTriaged = 18 Kinds.
    # + Loop 2: ConductorTickCompleted, WorkItemStageChanged,
    #   WorkItemBlocked = 21.
    # + Resilienz: RunResumeScheduled = 22.
    assert len(EventKind) == 22
    for kind in EventKind:
        assert kind in _PAYLOAD_REGISTRY, f"missing schema for {kind}"


def test_build_event_stamps_payload_schema_version() -> None:
    evt = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="r1",
        payload=RunStartedPayload(
            trigger="manual",
            strategy="sequential",
            config_hash="sha256:cfg",
        ),
        **COMMON,
    )
    assert evt.payload_schema_version == "1.0"
    assert evt.kind is EventKind.RUN_STARTED


def test_plan_proposed_schema_is_v1_1_with_additive_fields() -> None:
    """PlanProposed wurde additiv auf 1.1 erweitert (subtasks + agents_used).

    Alte 1.0-Events (ohne die neuen Felder) müssen weiterhin laden — die
    neuen Felder haben Defaults."""
    from forge_core.events.kinds.plan import PlanProposedPayload, PlanSubtask

    # Stamp: Registry führt 1.1.
    evt = build_event(
        kind=EventKind.PLAN_PROPOSED,
        run_id="r1",
        generation_id="g1",
        payload=PlanProposedPayload(
            architect_turns=8,
            subtask_count=2,
            subtasks=[
                PlanSubtask(index=1, title="A", status="done"),
                PlanSubtask(index=2, title="B"),
            ],
            agents_used=["architect", "developer", "tester"],
        ),
        **COMMON,
    )
    assert evt.payload_schema_version == "1.1"
    assert evt.payload["subtasks"][0]["status"] == "done"
    assert evt.payload["subtasks"][1]["status"] == "unknown"
    assert evt.payload["agents_used"] == ["architect", "developer", "tester"]

    # Alt-Event (Schema 1.0, nur Altfelder) lädt via Modell mit Defaults.
    legacy = PlanProposedPayload.model_validate(
        {"architect_turns": 4, "subtask_count": 1, "risk_level": "low"}
    )
    assert legacy.subtasks == []
    assert legacy.agents_used == []


def test_build_event_validates_dict_payload() -> None:
    evt = build_event(
        kind=EventKind.RUN_FINISHED,
        run_id="r1",
        payload={
            "decision": "pr_created",
            "generations_count": 5,
            "final_score": 0.81,
            "total_cost_usd": "0.94",
        },
        **COMMON,
    )
    assert evt.payload["decision"] == "pr_created"
    assert evt.payload["generations_count"] == 5


def test_build_event_rejects_wrong_payload_type() -> None:
    with pytest.raises(ValueError):
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r1",
            payload=RunFinishedPayload(decision="pr_created", generations_count=0),
            **COMMON,
        )


def test_artifact_hash_must_be_sha256_prefixed_64_hex() -> None:
    with pytest.raises(ValueError):
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r1",
            payload=RunStartedPayload(
                trigger="manual", strategy="sequential", config_hash="sha256:cfg"
            ),
            artifacts={"bad": "deadbeef"},
            **COMMON,
        )

    with pytest.raises(ValueError):
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r1",
            payload=RunStartedPayload(
                trigger="manual", strategy="sequential", config_hash="sha256:cfg"
            ),
            artifacts={"bad": "sha256:not-hex"},
            **COMMON,
        )


def test_event_is_frozen() -> None:
    evt = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="r1",
        payload=RunStartedPayload(
            trigger="manual", strategy="sequential", config_hash="sha256:cfg"
        ),
        **COMMON,
    )
    with pytest.raises((TypeError, ValueError)):
        evt.run_id = "other"  # type: ignore[misc]


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r1",
            payload=RunStartedPayload(
                trigger="manual", strategy="sequential", config_hash="sha256:cfg"
            ),
            ts=datetime(2026, 1, 1, 12, 0),  # naive
            **COMMON,
        )


def test_non_utc_datetime_rejected() -> None:
    cest = timezone(timedelta(hours=2))
    with pytest.raises(ValueError):
        build_event(
            kind=EventKind.RUN_STARTED,
            run_id="r1",
            payload=RunStartedPayload(
                trigger="manual", strategy="sequential", config_hash="sha256:cfg"
            ),
            ts=datetime(2026, 1, 1, 12, 0, tzinfo=cest),
            **COMMON,
        )


def test_default_event_id_is_ulid_sortable() -> None:
    e1 = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="r1",
        payload=RunStartedPayload(
            trigger="manual", strategy="sequential", config_hash="sha256:cfg"
        ),
        **COMMON,
    )
    e2 = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="r1",
        payload=RunStartedPayload(
            trigger="manual", strategy="sequential", config_hash="sha256:cfg"
        ),
        **COMMON,
    )
    # ULIDs erzeugt nacheinander sind chronologisch sortierbar als string
    assert e1.event_id < e2.event_id


def test_cost_usd_preserves_precision() -> None:
    evt = build_event(
        kind=EventKind.RUN_FINISHED,
        run_id="r1",
        payload=RunFinishedPayload(
            decision="pr_created",
            generations_count=1,
            total_cost_usd=Decimal("0.123456"),
        ),
        cost_usd=Decimal("0.0185"),
        **COMMON,
    )
    assert evt.cost_usd == Decimal("0.0185")
