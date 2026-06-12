"""Inkrement 3: Conductor leitet fällige Resumes rein aus dem Event-Strom ab.

Mantra 3: Der Runner plant den Resume nie selbst — der Conductor (Loop 2)
entscheidet das WANN über ``derive_pending_resumes`` (pure, replay-fähig,
analog zu ``derive_signals``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from forge_cli.conductor import derive_pending_resumes
from forge_core.events import EventKind

T0 = datetime(2026, 6, 12, 10, 0, tzinfo=UTC)


def _ev(kind: EventKind, run_id: str, ts: datetime, **payload: object) -> SimpleNamespace:
    return SimpleNamespace(kind=kind, run_id=run_id, ts=ts, payload=payload)


def _anchor(run_id: str, ts: datetime, reset_at: datetime | None, **extra: object):
    return _ev(
        EventKind.RUN_RESUME_SCHEDULED,
        run_id,
        ts,
        resume_session_id=extra.get("session", "sess-1"),
        resume_at=reset_at.isoformat() if reset_at is not None else None,
        resume_worktree=extra.get("worktree", "/wt/" + run_id),
        issue_number=extra.get("issue_number", 7),
    )


def test_due_resume_is_returned() -> None:
    events = [
        _ev(EventKind.RUN_STARTED, "R1", T0, issue_number=7),
        _anchor("R1", T0 + timedelta(minutes=5), reset_at=T0 + timedelta(hours=1)),
    ]
    orders = derive_pending_resumes(events, now=T0 + timedelta(hours=2))
    assert len(orders) == 1
    assert orders[0].run_id == "R1"
    assert orders[0].resume_session_id == "sess-1"
    assert orders[0].issue_number == 7
    assert orders[0].worktree == "/wt/R1"


def test_not_due_until_reset_time() -> None:
    events = [_anchor("R1", T0, reset_at=T0 + timedelta(hours=3))]
    # now liegt VOR dem reset → noch nicht fällig.
    assert derive_pending_resumes(events, now=T0 + timedelta(hours=1)) == []


def test_already_resumed_is_not_redispatched() -> None:
    # Ein RunStarted NACH dem Anker (gleiche run_id) = der Resume lief bereits.
    events = [
        _anchor("R1", T0, reset_at=T0),
        _ev(EventKind.RUN_STARTED, "R1", T0 + timedelta(minutes=1)),
    ]
    assert derive_pending_resumes(events, now=T0 + timedelta(hours=1)) == []


def test_run_started_before_anchor_does_not_count_as_resumed() -> None:
    # Das ursprüngliche RunStarted (vor dem Limit) darf den Anker nicht "verbrauchen".
    events = [
        _ev(EventKind.RUN_STARTED, "R1", T0 - timedelta(minutes=10)),
        _anchor("R1", T0, reset_at=T0),
    ]
    orders = derive_pending_resumes(events, now=T0 + timedelta(hours=1))
    assert [o.run_id for o in orders] == ["R1"]


def test_unparseable_reset_time_is_manual_only() -> None:
    # resume_at None (Reset-Zeit nicht parsebar) → nie auto-dispatcht.
    events = [_anchor("R1", T0, reset_at=None)]
    assert derive_pending_resumes(events, now=T0 + timedelta(days=1)) == []


def test_latest_anchor_per_run_wins() -> None:
    # Zwei Limits auf demselben Run; nur der jüngste Anker zählt. Wenn nach dem
    # jüngsten kein RunStarted kam, ist er fällig.
    events = [
        _anchor("R1", T0, reset_at=T0),
        _ev(EventKind.RUN_STARTED, "R1", T0 + timedelta(minutes=1)),  # 1. Resume lief
        _anchor("R1", T0 + timedelta(minutes=2), reset_at=T0 + timedelta(minutes=3)),
    ]
    orders = derive_pending_resumes(events, now=T0 + timedelta(hours=1))
    assert [o.run_id for o in orders] == ["R1"]  # 2. Limit noch offen → fällig


def test_no_anchors_returns_empty() -> None:
    events = [_ev(EventKind.RUN_STARTED, "R1", T0)]
    assert derive_pending_resumes(events, now=T0) == []
