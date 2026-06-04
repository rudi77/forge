"""Conductor — die Tick-Planung (Loop 2).

Reine Logik: aus dem aktuellen Stand der Work-Items (Stage + Dependencies +
Event-Signalen) errechnet ``plan_tick`` die Differenz, die der Conductor
effektieren soll — welche Stage-Labels wandern, welche Items dispatcht werden,
welche blockiert sind. KEIN I/O: das Lesen (Board/Events) und das Effektieren
(Labels setzen, Runs dispatchen) macht die dünne Wiring-Schicht im board-loop.

So bleibt das Fließband-Hirn deterministisch und voll testbar — und Mantra 3
gewahrt: der Conductor plant über Work-Items, nie über Loop-Logik.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from forge_core.events import EventKind

from forge_cli.dependencies import find_cycle, unmet_dependencies
from forge_cli.stages import Stage, StageSignals, advance


@dataclass(frozen=True)
class WorkItem:
    """Der Conductor-Blick auf ein Issue zu Tick-Beginn."""

    number: int
    stage: Stage
    depends_on: tuple[int, ...] = ()
    signals: StageSignals = field(default_factory=StageSignals)


@dataclass(frozen=True)
class StageTransition:
    number: int
    from_stage: Stage
    to_stage: Stage
    reason: str


@dataclass(frozen=True)
class Blocked:
    number: int
    kind: str  # "deps" | "cycle"
    blocked_by: tuple[int, ...]
    reason: str


@dataclass(frozen=True)
class TickPlan:
    """Was der Conductor in diesem Tick tun soll."""

    transitions: list[StageTransition] = field(default_factory=list)
    dispatch: list[int] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)


def plan_tick(items: list[WorkItem], *, capacity: int) -> TickPlan:
    """Errechnet die Tick-Differenz: Übergänge, Dispatch-Set, Blockaden.

    Ablauf (deterministisch, Items nach Nummer sortiert):
      1. ADVANCE — event-getriebene Auto-Übergänge (design→ready bei Plan,
         in-dev→qa bei PR, qa→release bei Merge). Ein Item, das in diesem Tick
         neu nach ``ready`` wandert, wird ERST im nächsten Tick dispatcht
         (genau ein Übergang pro Item pro Tick).
      2. CYCLES — Items in einem Dependency-Zyklus werden blockiert.
      3. RESOLVE — Items, die schon zu Tick-Beginn in ``ready`` stehen: alle
         Dependencies ``done`` → Dispatch-Kandidat; sonst blocked(``deps``).
      4. DISPATCH — Kandidaten nach Nummer, bis ``capacity`` erschöpft; jeder
         erhält einen ``ready→in-dev``-Übergang (Grund ``dispatched``).
    """
    items = sorted(items, key=lambda w: w.number)
    known = {w.number for w in items}
    effective: dict[int, Stage] = {w.number: w.stage for w in items}

    transitions: list[StageTransition] = []
    blocked: list[Blocked] = []

    # 1. ADVANCE -------------------------------------------------------
    for w in items:
        nxt, reason = advance(w.stage, w.signals)
        if nxt != w.stage:
            transitions.append(StageTransition(w.number, w.stage, nxt, reason))
            effective[w.number] = nxt

    # 2. CYCLES --------------------------------------------------------
    graph = {w.number: [d for d in w.depends_on] for w in items}
    cycle = find_cycle(graph)
    cycle_nodes: set[int] = set(cycle or [])
    for n in sorted(cycle_nodes):
        blocked.append(
            Blocked(n, "cycle", tuple(sorted(cycle_nodes - {n})), "dependency cycle")
        )

    done = {n for n, st in effective.items() if st == Stage.DONE}

    # 3. RESOLVE (nur Items, die zu Tick-Beginn READY waren) -----------
    dispatch: list[int] = []
    for w in items:
        if w.number in cycle_nodes:
            continue
        if w.stage != Stage.READY:
            continue
        unmet = unmet_dependencies(
            w.number, list(w.depends_on), done=done, known=known
        )
        if unmet:
            blocked.append(
                Blocked(
                    w.number,
                    "deps",
                    tuple(unmet),
                    f"waiting on {', '.join(f'#{d}' for d in unmet)}",
                )
            )
        else:
            dispatch.append(w.number)

    # 4. DISPATCH (kapazitätsbegrenzt) ---------------------------------
    selected = dispatch[: max(0, capacity)]
    for n in selected:
        transitions.append(
            StageTransition(n, Stage.READY, Stage.IN_DEV, "dispatched")
        )

    return TickPlan(transitions=transitions, dispatch=selected, blocked=blocked)


@dataclass(frozen=True)
class ConductorTickResult:
    """Was ein Conductor-Tick effektiv getan hat."""

    transitions: int = 0
    dispatched: int = 0
    blocked: int = 0


# Effekt-Callables, die die Wiring-Schicht (board-loop) injiziert. So bleibt
# `run_conductor_tick` deterministisch testbar — die Tests übergeben Recorder
# statt echter gh-Aufrufe.
SetStageFn = Callable[[StageTransition], None]
DispatchFn = Callable[[int], None]
OnBlockedFn = Callable[[Blocked], None]


def run_conductor_tick(
    *,
    items: list[WorkItem],
    capacity: int,
    set_stage: SetStageFn,
    dispatch: DispatchFn,
    on_blocked: OnBlockedFn | None = None,
) -> ConductorTickResult:
    """Plant einen Tick und effektiert ihn über die injizierten Callables.

    Reihenfolge ist sicherheitsrelevant: **erst** alle Stage-Übergänge
    effektieren (inkl. ``ready→in-dev`` für Dispatch-Kandidaten), **dann**
    dispatchen. Crasht ein Dispatch, steht das Item schon auf ``in-dev`` und
    wird im nächsten Tick nicht erneut dispatcht (at-most-once).
    """
    plan = plan_tick(items, capacity=capacity)
    for transition in plan.transitions:
        set_stage(transition)
    for number in plan.dispatch:
        dispatch(number)
    if on_blocked is not None:
        for blocked in plan.blocked:
            on_blocked(blocked)
    return ConductorTickResult(
        transitions=len(plan.transitions),
        dispatched=len(plan.dispatch),
        blocked=len(plan.blocked),
    )


def derive_signals(events: list, issue_number: int) -> StageSignals:
    """Leitet die Stage-Signale eines Work-Items aus dem Event-Strom ab.

    Rein und replay-fähig: korreliert ``RunStarted.issue_number`` → ``run_id``
    und liest daraus Plan/PR/Merge-Signale. ``events`` ist eine Liste von
    ``Event``-Objekten (``.kind``, ``.run_id``, ``.payload``-dict).

      - has_plan      : ein PlanProposed (ohne insufficient_context) in einem
                        Run dieses Issues.
      - has_open_pr   : ein PRCreated in einem Run dieses Issues.
      - has_merged_pr : ein PRMerged, dessen pr_number zu einem PRCreated
                        dieses Issues gehört.
    """
    run_ids = {
        e.run_id
        for e in events
        if e.kind == EventKind.RUN_STARTED
        and (e.payload or {}).get("issue_number") == issue_number
    }
    if not run_ids:
        return StageSignals()

    has_plan = any(
        e.kind == EventKind.PLAN_PROPOSED
        and e.run_id in run_ids
        and not (e.payload or {}).get("insufficient_context", False)
        for e in events
    )
    pr_numbers = {
        (e.payload or {}).get("pr_number")
        for e in events
        if e.kind == EventKind.PR_CREATED and e.run_id in run_ids
    }
    pr_numbers.discard(None)
    has_open_pr = bool(pr_numbers)
    has_merged_pr = any(
        e.kind == EventKind.PR_MERGED
        and (e.payload or {}).get("pr_number") in pr_numbers
        for e in events
    )
    return StageSignals(
        has_plan=has_plan,
        has_open_pr=has_open_pr,
        has_merged_pr=has_merged_pr,
    )
