"""Unit-Tests für interne Helpers in forge_cli.run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge_cli.run import _load_latest_plan, _split_agents
from forge_core.blobs import BlobStore
from forge_core.events import EventKind, build_event
from forge_core.events.kinds.plan import PlanProposedPayload
from forge_core.store import EventStore

_COMMON = dict(
    project="testproj",
    project_fingerprint="sha256:test",
    factory_version="git:test",
    spec_version="1.0",
)


@dataclass
class _StubContext:
    """Minimaler ForgeContext-Stub — Helper braucht nur open_blobs()."""

    blobs_path: Path

    def open_blobs(self) -> BlobStore:
        return BlobStore(self.blobs_path)


def test_load_latest_plan_returns_blob_text(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        plan_md = "## Plan\n\n1. Fix the calc bug\n2. Add a regression test\n## Risk\nlow\n"
        plan_hash = blobs.put_text(plan_md)
        evt = build_event(
            kind=EventKind.PLAN_PROPOSED,
            run_id="r1",
            generation_id="g1",
            payload=PlanProposedPayload(architect_turns=4, subtask_count=2, risk_level="low"),
            artifacts={"plan": plan_hash},
            **_COMMON,
        )
        store.append(evt)
        ctx = _StubContext(blobs_path=tmp_path / "blobs")
        result = _load_latest_plan(store, ctx, run_id="r1")
        assert result == plan_md
    finally:
        store.close()


def test_load_latest_plan_picks_last_when_multiple(tmp_path: Path) -> None:
    """Bei mehreren PlanProposed-Events (eines pro Generation) wird der
    letzte gewählt — der zur KEEP-Generation gehört, deren Diff in den
    PR fließt."""
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    try:
        plan_first = "## Plan v1\nfirst try"
        plan_last = "## Plan v2\nbetter try"
        hash_first = blobs.put_text(plan_first)
        hash_last = blobs.put_text(plan_last)
        for idx, (h, gen) in enumerate(
            [(hash_first, "g1"), (hash_last, "g2")]
        ):
            store.append(
                build_event(
                    kind=EventKind.PLAN_PROPOSED,
                    run_id="r1",
                    generation_id=gen,
                    payload=PlanProposedPayload(architect_turns=idx + 1),
                    artifacts={"plan": h},
                    **_COMMON,
                )
            )
        ctx = _StubContext(blobs_path=tmp_path / "blobs")
        result = _load_latest_plan(store, ctx, run_id="r1")
        assert result == plan_last
    finally:
        store.close()


def test_load_latest_plan_returns_none_when_no_plan_event(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    try:
        ctx = _StubContext(blobs_path=tmp_path / "blobs")
        assert _load_latest_plan(store, ctx, run_id="r-empty") is None
    finally:
        store.close()


def test_load_latest_plan_returns_none_when_blob_missing(tmp_path: Path) -> None:
    """Wenn der Plan-Hash auf einen nicht-existenten Blob zeigt (CAS-GC,
    Manual-Delete), liefert der Helper sauber None — nicht crashen."""
    store = EventStore(tmp_path / "events.duckdb")
    try:
        evt = build_event(
            kind=EventKind.PLAN_PROPOSED,
            run_id="r1",
            generation_id="g1",
            payload=PlanProposedPayload(architect_turns=1),
            artifacts={"plan": "sha256:" + "0" * 64},
            **_COMMON,
        )
        store.append(evt)
        ctx = _StubContext(blobs_path=tmp_path / "blobs")
        assert _load_latest_plan(store, ctx, run_id="r1") is None
    finally:
        store.close()


# --- _split_agents ----------------------------------------------------


def test_split_agents_parses_comma_list() -> None:
    assert _split_agents("architect,developer,tester") == [
        "architect",
        "developer",
        "tester",
    ]
    # Whitespace und leere Felder werden getrimmt/verworfen.
    assert _split_agents(" developer , tester ,, ") == ["developer", "tester"]


def test_split_agents_none_and_empty_return_none() -> None:
    assert _split_agents(None) is None
    assert _split_agents("") is None
    assert _split_agents("   ") is None
    assert _split_agents(",, ,") is None
