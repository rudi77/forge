"""Tests für forge_cli.watch.

Pure Kernfunktionen (Discovery, Rendering, Loop) ohne IO; ``collect_worktree_state``
gegen ein echtes Temp-Git-Repo (schnell, vgl. test_worktrees.py).
"""

from __future__ import annotations

import io
import subprocess
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from forge_cli.watch import (
    WorktreeState,
    _border_for,
    _cost_label,
    _events_show_finished,
    _generations_label,
    _phase_label,
    _watch_loop,
    collect_worktree_state,
    discover_active_run,
    format_age,
    render_watch_view,
)
from forge_core.events import Event, EventKind
from rich.console import Console
from rich.panel import Panel

_NOW = datetime(2026, 6, 11, 12, 0, 0, tzinfo=UTC)


def _evt(
    kind: EventKind,
    *,
    ts: datetime,
    cost: str = "0",
    success: bool | None = None,
    generation_id: str | None = None,
) -> Event:
    return Event(
        run_id="01RUN",
        generation_id=generation_id,
        project="demo",
        project_fingerprint="sha256:" + "0" * 64,
        factory_version="git:abc123",
        spec_version="0.5",
        ts=ts,
        kind=kind,
        payload_schema_version="1.0",
        payload={},
        cost_usd=Decimal(cost),
        success=success,
    )


def _render_to_text(panel: Panel) -> str:
    buf = io.StringIO()
    Console(file=buf, width=120, legacy_windows=False).print(panel)
    return buf.getvalue()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )


# --- format_age ---------------------------------------------------------


@pytest.mark.parametrize(
    "seconds,expected",
    [(0, "0s"), (42, "42s"), (61, "1m 1s"), (184, "3m 4s"), (3720, "1h 2m")],
)
def test_format_age(seconds: float, expected: str) -> None:
    assert format_age(seconds) == expected


# --- discover_active_run ------------------------------------------------


def test_discover_active_run_empty_returns_none(tmp_path: Path) -> None:
    assert discover_active_run(tmp_path / "nope") is None
    (tmp_path / "worktrees").mkdir()
    assert discover_active_run(tmp_path / "worktrees") is None


def test_discover_active_run_picks_newest(tmp_path: Path) -> None:
    root = tmp_path / "worktrees"
    root.mkdir()
    old = root / "01OLD"
    new = root / "01NEW"
    old.mkdir()
    new.mkdir()
    # mtime explizit setzen, damit "new" jünger ist (kein Timing-Flake).
    import os

    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert discover_active_run(root) == "01NEW"


# --- collect_worktree_state (echtes git) --------------------------------


def test_collect_worktree_state_active_with_changes(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-b", "main")
    _git(wt, "config", "user.email", "t@forge.local")
    _git(wt, "config", "user.name", "forge-test")
    (wt / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "forge: gen 1 | composite +0.04")
    # Uncommittete Änderung → taucht in git status auf.
    (wt / "b.py").write_text("y = 2\n", encoding="utf-8")

    now = datetime.now(UTC)
    state = collect_worktree_state(wt, "01RUN", now=now, idle_threshold_s=30.0)

    assert state.exists is True
    assert state.forge_commits == 1
    assert state.changed_count >= 1
    assert any("b.py" in p for p in state.changed_files)
    assert state.newest_file is not None
    assert state.liveness == "active"  # gerade geschrieben


def test_collect_worktree_state_idle_when_now_far_ahead(tmp_path: Path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    _git(wt, "init", "-b", "main")
    (wt / "a.py").write_text("x = 1\n", encoding="utf-8")

    far_future = datetime.now(UTC) + timedelta(hours=2)
    state = collect_worktree_state(wt, "01RUN", now=far_future, idle_threshold_s=30.0)
    assert state.exists is True
    assert state.liveness == "idle"
    assert state.last_write_age_s is not None and state.last_write_age_s > 30.0


def test_collect_worktree_state_missing_dir(tmp_path: Path) -> None:
    state = collect_worktree_state(tmp_path / "ghost", "01RUN", now=_NOW, idle_threshold_s=30.0)
    assert state.exists is False
    assert state.liveness == "unknown"


# --- pure render helpers ------------------------------------------------


def _state(liveness: str = "active") -> WorktreeState:
    return WorktreeState(
        run_id="01RUN",
        wt_path=Path("/tmp/wt"),
        exists=True,
        changed_files=["src/a.py"],
        changed_count=1,
        forge_commits=2,
        newest_file="src/a.py",
        last_write_age_s=5.0,
        liveness=liveness,
    )


def test_border_for() -> None:
    assert _border_for(_state("active"), finished=False) == "green"
    assert _border_for(_state("idle"), finished=False) == "yellow"
    assert _border_for(_state("unknown"), finished=False) == "cyan"
    assert _border_for(_state("active"), finished=True) == "cyan"


def test_phase_label() -> None:
    assert "gesperrt" in _phase_label(None)
    assert "keine Events" in _phase_label([])
    evt = _evt(EventKind.PROPOSAL_REQUESTED, ts=_NOW)
    assert _phase_label([evt]) == "ProposalRequested"


def test_cost_label_sums() -> None:
    events = [
        _evt(EventKind.PROPOSAL_RECEIVED, ts=_NOW, cost="0.10"),
        _evt(EventKind.EVAL_FINISHED, ts=_NOW, cost="0.05"),
    ]
    assert _cost_label(events) == "$0.1500"
    assert "n/a" in _cost_label(None)


def test_generations_label() -> None:
    events = [
        _evt(EventKind.GENERATION_STARTED, ts=_NOW),
        _evt(EventKind.GENERATION_STARTED, ts=_NOW),
    ]
    label = _generations_label(events, _state())
    assert "2 gestartet" in label and "gehalten" in label
    # Ohne Events fällt das Label auf die git-log-Zählung zurück.
    assert "2 gehalten" in _generations_label(None, _state())


def test_events_show_finished() -> None:
    assert _events_show_finished(None) is False
    assert _events_show_finished([]) is False
    assert _events_show_finished([_evt(EventKind.RUN_STARTED, ts=_NOW)]) is False
    assert _events_show_finished([_evt(EventKind.RUN_FINISHED, ts=_NOW)]) is True


# --- render_watch_view (integration) ------------------------------------


def test_render_watch_view_with_events() -> None:
    events = [
        _evt(EventKind.RUN_STARTED, ts=_NOW - timedelta(minutes=5)),
        _evt(EventKind.PROPOSAL_REQUESTED, ts=_NOW - timedelta(minutes=4)),
    ]
    panel = render_watch_view("01RUN", events, _state("active"), now=_NOW)
    assert isinstance(panel, Panel)
    text = _render_to_text(panel)
    assert "01RUN" in text
    assert "ProposalRequested" in text
    assert "worktree" in text
    assert "aktiv" in text


def test_render_watch_view_degraded_when_db_locked() -> None:
    # events=None → DB gesperrt; muss ohne Crash die Degradations-Notiz zeigen.
    panel = render_watch_view("01RUN", None, _state("active"), now=_NOW)
    text = _render_to_text(panel)
    assert "01RUN" in text
    assert "gesperrt" in text or "parallelen Zugriff" in text


def test_render_watch_view_missing_worktree_finished() -> None:
    events = [
        _evt(EventKind.RUN_STARTED, ts=_NOW - timedelta(minutes=5)),
        _evt(EventKind.RUN_FINISHED, ts=_NOW),
    ]
    state = WorktreeState(run_id="01RUN", wt_path=Path("/tmp/gone"), exists=False)
    panel = render_watch_view("01RUN", events, state, now=_NOW)
    assert _border_for(state, finished=True) == "cyan"
    text = _render_to_text(panel)
    assert "beendet" in text


# --- _watch_loop --------------------------------------------------------


def test_watch_loop_max_ticks_sleeps_between_only() -> None:
    sleeps: list[float] = []
    renders: list[Panel] = []
    panel = render_watch_view("01RUN", [], _state(), now=_NOW)

    def tick() -> tuple[Panel, list[Event] | None]:
        return panel, []

    count = _watch_loop(
        tick,
        interval_s=2.0,
        sleep=sleeps.append,
        should_stop=lambda: False,
        on_render=renders.append,
        max_ticks=3,
    )
    assert count == 3
    assert len(renders) == 3
    # Kein Schlaf nach dem letzten Tick → genau 2 Sleeps zwischen 3 Ticks.
    assert sleeps == [2.0, 2.0]


def test_watch_loop_stops_when_run_finished() -> None:
    renders: list[Panel] = []
    panel = render_watch_view("01RUN", None, _state(), now=_NOW)
    finished_events = [_evt(EventKind.RUN_FINISHED, ts=_NOW)]

    def tick() -> tuple[Panel, list[Event] | None]:
        return panel, finished_events

    count = _watch_loop(
        tick,
        interval_s=1.0,
        sleep=lambda _s: None,
        should_stop=lambda: False,
        on_render=renders.append,
        max_ticks=10,
        is_done=_events_show_finished,
    )
    assert count == 1  # bricht nach dem ersten Tick ab (RunFinished gesehen)


def test_watch_loop_should_stop_immediately() -> None:
    renders: list[Panel] = []
    count = _watch_loop(
        lambda: (render_watch_view("01RUN", [], _state(), now=_NOW), []),
        interval_s=1.0,
        sleep=lambda _s: None,
        should_stop=lambda: True,
        on_render=renders.append,
        max_ticks=5,
    )
    assert count == 0
    assert renders == []
