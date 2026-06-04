"""Tests für die Heartbeat-Engine (forge_cli.heartbeat)."""

from __future__ import annotations

from forge_cli.heartbeat import HeartbeatStats, TickResult, run_heartbeat


def test_runs_until_max_ticks_and_sleeps_between_only() -> None:
    sleeps: list[float] = []
    ticks: list[int] = []

    def tick_fn(i: int) -> TickResult:
        ticks.append(i)
        return TickResult(dispatched=1)

    stats = run_heartbeat(
        tick_fn=tick_fn,
        interval_s=300,
        sleep=sleeps.append,
        should_stop=lambda: False,
        max_ticks=3,
    )
    assert isinstance(stats, HeartbeatStats)
    assert ticks == [0, 1, 2]
    assert stats.ticks == 3
    assert stats.total_dispatched == 3
    assert stats.stopped_reason == "max_ticks"
    # Kein Schlaf nach dem letzten Tick → genau 2 Sleeps zwischen 3 Ticks.
    assert sleeps == [300, 300]


def test_stop_signal_ends_loop_before_first_tick() -> None:
    calls: list[int] = []
    stats = run_heartbeat(
        tick_fn=lambda i: calls.append(i) or TickResult(),
        interval_s=1,
        sleep=lambda _s: None,
        should_stop=lambda: True,
        max_ticks=10,
    )
    assert calls == []
    assert stats.ticks == 0
    assert stats.stopped_reason == "stop_signal"


def test_stop_signal_after_n_ticks() -> None:
    state = {"n": 0}

    def should_stop() -> bool:
        # Stoppt, sobald 2 Ticks gelaufen sind.
        return state["n"] >= 2

    def tick_fn(_i: int) -> TickResult:
        state["n"] += 1
        return TickResult(scheduled=1)

    stats = run_heartbeat(
        tick_fn=tick_fn,
        interval_s=1,
        sleep=lambda _s: None,
        should_stop=should_stop,
        max_ticks=100,
    )
    assert stats.ticks == 2
    assert stats.total_scheduled == 2
    assert stats.stopped_reason == "stop_signal"


def test_stop_on_bail() -> None:
    def tick_fn(i: int) -> TickResult:
        return TickResult(dispatched=1, bailed=(i == 1))

    emitted: list[tuple[int, bool]] = []
    stats = run_heartbeat(
        tick_fn=tick_fn,
        interval_s=1,
        sleep=lambda _s: None,
        should_stop=lambda: False,
        emit=lambda i, r: emitted.append((i, r.bailed)),
        max_ticks=10,
        stop_on_bail=True,
    )
    assert stats.ticks == 2
    assert stats.stopped_reason == "bailed"
    assert emitted == [(0, False), (1, True)]


def test_emit_called_per_tick() -> None:
    seen: list[int] = []
    run_heartbeat(
        tick_fn=lambda i: TickResult(),
        interval_s=1,
        sleep=lambda _s: None,
        should_stop=lambda: False,
        emit=lambda i, r: seen.append(i),
        max_ticks=3,
    )
    assert seen == [0, 1, 2]
