"""Heartbeat — Dauerbetrieb der Fabrik (Conductor Phase B).

Macht aus dem einmaligen ``board-loop`` einen „rund um die Uhr"-Prozess: eine
Endlosschleife aus **Ticks**. Was ein Tick konkret tut (Board-Pass + fällige
schedule-Trigger), gibt der Caller als ``tick_fn`` herein — die Engine selbst
ist reine Schleifen-Mechanik und mit injizierten Dependencies (``sleep``,
``should_stop``, ``emit``) vollständig ohne echtes ``time.sleep`` testbar.

Mantra 3: der Heartbeat steht ÜBER der Loop. Er taktet das Dispatchen von
Runs, greift aber nie in den Runner, das Scoring oder die Gates ein.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass
class TickResult:
    """Was ein einzelner Tick bewirkt hat."""

    dispatched: int = 0
    """Work-Items, die als Run dispatcht wurden."""

    scheduled: int = 0
    """Fällige schedule-Trigger, die feuerten."""

    blocked: int = 0
    """Items, die nicht voran konnten (Phase C; in Phase B immer 0)."""

    skipped: int = 0
    """Übersprungene Items (z.B. Triage-Filter)."""

    scheduled_resume_count: int = 0
    """Fällige Resume-Dispatches (vom Usage-Limit unterbrochene Runs)."""

    bailed: bool = False
    """True, wenn der Board-Pass abbrach (Cost-Cap/Guardrail/Fehler)."""


@dataclass
class HeartbeatStats:
    """Aggregat über alle gelaufenen Ticks einer Session."""

    ticks: int = 0
    total_dispatched: int = 0
    total_scheduled: int = 0
    stopped_reason: str = "stop_signal"
    """Warum die Schleife endete: ``stop_signal``, ``max_ticks`` oder
    ``bailed``."""


TickFn = Callable[[int], TickResult]
SleepFn = Callable[[float], None]
StopFn = Callable[[], bool]
EmitFn = Callable[[int, TickResult], None]


def run_heartbeat(
    *,
    tick_fn: TickFn,
    interval_s: float,
    sleep: SleepFn,
    should_stop: StopFn,
    emit: EmitFn | None = None,
    max_ticks: int | None = None,
    stop_on_bail: bool = False,
) -> HeartbeatStats:
    """Treibt die Heartbeat-Schleife.

    Pro Iteration: Stop-Bedingungen prüfen → ``tick_fn(index)`` → ``emit`` →
    erneut Stop prüfen (kein Schlaf nach dem letzten Tick) → ``sleep``.

    Parameter:
        tick_fn: bekommt den 0-basierten Tick-Index, liefert ein ``TickResult``.
        interval_s: Schlafdauer zwischen Ticks.
        sleep: injizierbar (Tests übergeben einen Recorder statt ``time.sleep``).
        should_stop: True → Schleife endet sauber (z.B. SIGINT-Flag).
        emit: optionaler Callback pro Tick (z.B. ConductorTickCompleted-Event).
        max_ticks: Obergrenze (Tests / endliche Läufe). None = unbegrenzt.
        stop_on_bail: bei ``TickResult.bailed`` die Schleife beenden.
    """
    stats = HeartbeatStats()
    while True:
        if should_stop():
            stats.stopped_reason = "stop_signal"
            break
        if max_ticks is not None and stats.ticks >= max_ticks:
            stats.stopped_reason = "max_ticks"
            break

        result = tick_fn(stats.ticks)
        if emit is not None:
            emit(stats.ticks, result)
        stats.ticks += 1
        stats.total_dispatched += result.dispatched
        stats.total_scheduled += result.scheduled

        if stop_on_bail and result.bailed:
            stats.stopped_reason = "bailed"
            break
        # Nach dem letzten erlaubten Tick nicht unnötig schlafen.
        if max_ticks is not None and stats.ticks >= max_ticks:
            stats.stopped_reason = "max_ticks"
            break
        if should_stop():
            stats.stopped_reason = "stop_signal"
            break
        sleep(interval_s)
    return stats
