"""Tests für die Conductor-Cores: Stages, Dependencies, Tick-Planung."""

from __future__ import annotations

from forge_cli.conductor import (
    Blocked,
    StageTransition,
    WorkItem,
    derive_signals,
    plan_tick,
    run_conductor_tick,
)
from forge_cli.dependencies import (
    find_cycle,
    parse_depends_on,
    unmet_dependencies,
)
from forge_cli.stages import (
    Stage,
    StageSignals,
    advance,
    can_transition,
    stage_of,
)

# --- stages ---------------------------------------------------------------


def test_stage_of_picks_furthest_pipeline_label() -> None:
    assert stage_of(["bug", "forge:design"]) == Stage.DESIGN
    assert stage_of(["forge:ready", "forge:in-dev"]) == Stage.IN_DEV
    assert stage_of(["bug"]) is None
    # blocked gewinnt immer.
    assert stage_of(["forge:qa", "forge:blocked"]) == Stage.BLOCKED


def test_advance_event_driven_transitions() -> None:
    assert advance(Stage.DESIGN, StageSignals(has_plan=True)) == (
        Stage.READY,
        "plan_proposed",
    )
    assert advance(Stage.IN_DEV, StageSignals(has_open_pr=True)) == (
        Stage.QA,
        "pr_created",
    )
    assert advance(Stage.QA, StageSignals(has_merged_pr=True)) == (
        Stage.RELEASE,
        "pr_merged",
    )
    # Kein Signal → keine Bewegung.
    assert advance(Stage.DESIGN, StageSignals()) == (Stage.DESIGN, "")
    # ready→in-dev ist NICHT automatisch (Dispatch-Sache).
    assert advance(Stage.READY, StageSignals(has_open_pr=True)) == (
        Stage.READY,
        "",
    )


def test_allowed_transitions_guard() -> None:
    assert can_transition(Stage.DESIGN, Stage.READY)
    assert can_transition(Stage.READY, Stage.IN_DEV)
    assert not can_transition(Stage.DESIGN, Stage.DONE)
    assert not can_transition(Stage.DONE, Stage.READY)


# --- dependencies ---------------------------------------------------------


def test_parse_depends_on_variants() -> None:
    assert parse_depends_on("Depends-On: #12, #15") == [12, 15]
    assert parse_depends_on("depends on #3 and #4") == [3, 4]
    assert parse_depends_on("Blocked-By: #7") == [7]
    assert parse_depends_on("no deps here") == []
    assert parse_depends_on(None) == []
    # Dedup + Reihenfolge.
    assert parse_depends_on("Depends-On: #5\nBlocked-By: #5, #9") == [5, 9]


def test_find_cycle() -> None:
    assert find_cycle({1: [2], 2: [3], 3: []}) is None
    cyc = find_cycle({1: [2], 2: [3], 3: [1]})
    assert cyc is not None and set(cyc) >= {1, 2, 3}
    # Unbekannte Dependency ist kein Zyklus.
    assert find_cycle({1: [99]}) is None


def test_unmet_dependencies() -> None:
    assert unmet_dependencies(1, [2, 3], done={2, 3}, known={1, 2, 3}) == []
    assert unmet_dependencies(1, [2, 3], done={2}, known={1, 2, 3}) == [3]
    # Unbekannte Dependency gilt als unerfüllt (konservativ).
    assert unmet_dependencies(1, [99], done=set(), known={1}) == [99]


# --- plan_tick ------------------------------------------------------------


def _wi(number: int, stage: Stage, *, deps=(), signals=None) -> WorkItem:
    return WorkItem(
        number=number,
        stage=stage,
        depends_on=tuple(deps),
        signals=signals or StageSignals(),
    )


def test_plan_tick_auto_advance_then_no_same_tick_dispatch() -> None:
    # design + plan → ready (transition), aber NICHT im selben Tick dispatcht.
    plan = plan_tick(
        [_wi(1, Stage.DESIGN, signals=StageSignals(has_plan=True))], capacity=5
    )
    assert plan.transitions == [
        StageTransition(1, Stage.DESIGN, Stage.READY, "plan_proposed")
    ]
    assert plan.dispatch == []


def test_plan_tick_dispatches_ready_without_deps() -> None:
    plan = plan_tick([_wi(1, Stage.READY)], capacity=5)
    assert plan.dispatch == [1]
    assert StageTransition(1, Stage.READY, Stage.IN_DEV, "dispatched") in (
        plan.transitions
    )


def test_plan_tick_blocks_ready_with_unmet_deps() -> None:
    # #2 hängt von #1 (noch nicht done) ab → blocked, nicht dispatcht.
    items = [_wi(1, Stage.IN_DEV), _wi(2, Stage.READY, deps=[1])]
    plan = plan_tick(items, capacity=5)
    assert plan.dispatch == []
    assert any(b.number == 2 and b.kind == "deps" for b in plan.blocked)


def test_plan_tick_dispatches_when_dep_done() -> None:
    items = [_wi(1, Stage.DONE), _wi(2, Stage.READY, deps=[1])]
    plan = plan_tick(items, capacity=5)
    assert plan.dispatch == [2]


def test_plan_tick_capacity_limits_dispatch() -> None:
    items = [_wi(n, Stage.READY) for n in (1, 2, 3)]
    plan = plan_tick(items, capacity=1)
    assert plan.dispatch == [1]  # nach Nummer sortiert, nur 1 Slot


def test_plan_tick_cycle_blocks_members() -> None:
    items = [
        _wi(1, Stage.READY, deps=[2]),
        _wi(2, Stage.READY, deps=[1]),
    ]
    plan = plan_tick(items, capacity=5)
    assert plan.dispatch == []
    blocked_nums = {b.number for b in plan.blocked if b.kind == "cycle"}
    assert blocked_nums == {1, 2}


# --- run_conductor_tick (Effekt-Orchestrierung) ---------------------------


def test_run_conductor_tick_effects_transitions_before_dispatch() -> None:
    # #1 wird dispatcht (ready, deps done via #0 done); #2 advanced design→ready.
    items = [
        _wi(1, Stage.READY),
        _wi(2, Stage.DESIGN, signals=StageSignals(has_plan=True)),
    ]
    order: list[str] = []
    transitions: list[StageTransition] = []
    dispatched: list[int] = []
    blocked: list[Blocked] = []

    def set_stage(t: StageTransition) -> None:
        order.append(f"stage:{t.number}:{t.to_stage.value}")
        transitions.append(t)

    def dispatch(n: int) -> None:
        order.append(f"dispatch:{n}")
        dispatched.append(n)

    res = run_conductor_tick(
        items=items,
        capacity=5,
        set_stage=set_stage,
        dispatch=dispatch,
        on_blocked=blocked.append,
    )
    assert res.dispatched == 1
    assert dispatched == [1]
    # #2 advanced to ready, #1 advanced ready→in-dev (dispatch).
    assert res.transitions == 2
    # Sicherheit: der in-dev-Übergang von #1 kommt VOR dem dispatch von #1.
    assert order.index("stage:1:forge:in-dev") < order.index("dispatch:1")


def test_run_conductor_tick_reports_blocked() -> None:
    items = [_wi(2, Stage.READY, deps=[1])]  # #1 unbekannt → unerfüllt
    blocked: list[Blocked] = []
    res = run_conductor_tick(
        items=items,
        capacity=5,
        set_stage=lambda _t: None,
        dispatch=lambda _n: None,
        on_blocked=blocked.append,
    )
    assert res.dispatched == 0
    assert res.blocked == 1
    assert blocked[0].number == 2 and blocked[0].kind == "deps"


# --- derive_signals -------------------------------------------------------


class _Evt:
    """Minimaler Event-Stand-in (kind/run_id/payload)."""

    def __init__(self, kind, run_id, payload):
        self.kind = kind
        self.run_id = run_id
        self.payload = payload


def test_derive_signals_from_events() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PLAN_PROPOSED, "r1", {"insufficient_context": False}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
        _Evt(EK.PR_MERGED, "rX", {"pr_number": 100}),
        # Anderer Issue, darf nicht durchschlagen:
        _Evt(EK.RUN_STARTED, "r2", {"issue_number": 7}),
        _Evt(EK.PR_CREATED, "r2", {"pr_number": 200}),
    ]
    sig = derive_signals(events, 42)
    assert sig.has_plan is True
    assert sig.has_open_pr is True
    assert sig.has_merged_pr is True

    sig7 = derive_signals(events, 7)
    assert sig7.has_plan is False
    assert sig7.has_open_pr is True
    assert sig7.has_merged_pr is False  # PR 200 nie gemergt


def test_derive_signals_unknown_issue_is_empty() -> None:
    sig = derive_signals([], 999)
    assert sig == StageSignals()


def test_derive_signals_insufficient_context_is_no_plan() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 1}),
        _Evt(EK.PLAN_PROPOSED, "r1", {"insufficient_context": True}),
    ]
    assert derive_signals(events, 1).has_plan is False
