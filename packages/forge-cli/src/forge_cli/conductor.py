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
from forge_cli.stages import IN_PLACE_WORK_STAGES, Stage, StageSignals, advance


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
class DispatchOrder:
    """Ein zu dispatchender Run + die Stage, die das *Team* bestimmt.

    ``stage`` ist der Schlüssel zum „verschiedene-Teams"-Verhalten: die
    Wiring-Schicht wählt daraus das Roster und die Run-Art (``design`` →
    architect-Team, ``create_pr=False``, Output = Plan; ``in-dev`` → Dev-Team,
    ``create_pr=True``, Output = PR).
    """

    number: int
    stage: Stage


@dataclass(frozen=True)
class TickPlan:
    """Was der Conductor in diesem Tick tun soll."""

    transitions: list[StageTransition] = field(default_factory=list)
    dispatch: list[DispatchOrder] = field(default_factory=list)
    blocked: list[Blocked] = field(default_factory=list)


def plan_tick(items: list[WorkItem], *, capacity: int) -> TickPlan:
    """Errechnet die Tick-Differenz: Übergänge, Dispatch-Set, Blockaden.

    Ablauf (deterministisch, Items nach Nummer sortiert):
      1. ADVANCE — event-getriebene Auto-Übergänge (design→ready bei Plan,
         in-dev→qa bei PR, qa→release bei Merge). Ein Item, das in diesem Tick
         neu nach ``ready`` wandert, wird ERST im nächsten Tick dispatcht
         (genau ein Übergang pro Item pro Tick).
      2. CYCLES — Items in einem Dependency-Zyklus werden blockiert.
      3. RESOLVE — Dispatch-Kandidaten + ihr Team (Stage) bestimmen, jeweils
         dependency-gegated:
           * In-Place-Work-Stage (``design``), die ihren Auslöser noch nicht
             produziert hat → Kandidat mit ``stage=design`` (Team = architect,
             kein Stage-Wechsel — ``advance`` schreibt nächsten Tick fort).
           * Stage ``ready`` → Kandidat mit ``stage=in-dev`` (Team = Dev-Loop),
             erhält beim Dispatch den ``ready→in-dev``-Übergang.
         Unerfüllte Dependencies → blocked(``deps``), nicht dispatcht.
      4. DISPATCH — Kandidaten nach Nummer, bis ``capacity`` erschöpft. Eine
         gemeinsame Kapazität über *alle* Teams (sequenziell in v1).
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
            if nxt == Stage.BLOCKED:
                # Eskalation (stalled): zusätzlich als Blockade melden, damit
                # ein WorkItemBlocked-Event den Grund trägt — der Operator
                # sieht im Event-Strom UND am Label, dass er dran ist.
                blocked.append(Blocked(w.number, "stalled", (), reason))

    # 2. CYCLES --------------------------------------------------------
    graph = {w.number: [d for d in w.depends_on] for w in items}
    cycle = find_cycle(graph)
    cycle_nodes: set[int] = set(cycle or [])
    for n in sorted(cycle_nodes):
        blocked.append(
            Blocked(n, "cycle", tuple(sorted(cycle_nodes - {n})), "dependency cycle")
        )

    done = {n for n, st in effective.items() if st == Stage.DONE}

    # 3. RESOLVE — Kandidaten + Team, nur für Items, deren Stage zu
    # Tick-Beginn dispatch-fähig war (nicht erst durch ADVANCE dahin gewandert).
    candidates: list[DispatchOrder] = []
    for w in items:
        if w.number in cycle_nodes:
            continue
        eff = effective[w.number]
        if w.stage in IN_PLACE_WORK_STAGES and eff == w.stage:
            # design (noch ohne Plan): Team arbeitet in-place, kein Übergang.
            target = w.stage
        elif w.stage == Stage.READY:
            # Warteschlange → Dev-Team; Übergang folgt beim Dispatch.
            target = Stage.IN_DEV
        else:
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
            candidates.append(DispatchOrder(w.number, target))

    # 4. DISPATCH (gemeinsame Kapazität über alle Teams) ---------------
    selected = candidates[: max(0, capacity)]
    for order in selected:
        if order.stage == Stage.IN_DEV:
            transitions.append(
                StageTransition(order.number, Stage.READY, Stage.IN_DEV, "dispatched")
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
DispatchFn = Callable[[DispatchOrder], None]
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
    for order in plan.dispatch:
        dispatch(order)
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
      - last_run_finished_without_pr / _without_plan:
                        der JÜNGSTE Run des Issues (max ``ts`` des
                        RunStarted) hat ein RunFinished, aber kein
                        PRCreated bzw. keinen verwertbaren PlanProposed —
                        Eskalations-Auslöser (advance → blocked). Läuft der
                        jüngste Run noch (kein RunFinished), ist beides False.
    """
    started = [
        e
        for e in events
        if e.kind == EventKind.RUN_STARTED
        and (e.payload or {}).get("issue_number") == issue_number
    ]
    if not started:
        return StageSignals()
    run_ids = {e.run_id for e in started}

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

    latest_run_id = max(started, key=lambda e: e.ts).run_id
    latest_finished = any(
        e.kind == EventKind.RUN_FINISHED and e.run_id == latest_run_id
        for e in events
    )
    latest_has_pr = any(
        e.kind == EventKind.PR_CREATED and e.run_id == latest_run_id
        for e in events
    )
    latest_has_plan = any(
        e.kind == EventKind.PLAN_PROPOSED
        and e.run_id == latest_run_id
        and not (e.payload or {}).get("insufficient_context", False)
        for e in events
    )
    return StageSignals(
        has_plan=has_plan,
        has_open_pr=has_open_pr,
        has_merged_pr=has_merged_pr,
        last_run_finished_without_pr=latest_finished and not latest_has_pr,
        last_run_finished_without_plan=latest_finished and not latest_has_plan,
    )
