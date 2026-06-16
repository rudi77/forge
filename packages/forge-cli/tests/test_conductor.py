"""Tests für die Conductor-Cores: Stages, Dependencies, Tick-Planung."""

from __future__ import annotations

from datetime import UTC, datetime

from forge_cli.conductor import (
    MAX_DEV_RETRIES,
    Blocked,
    DispatchOrder,
    StageTransition,
    WorkItem,
    derive_dev_failure,
    derive_signals,
    plan_tick,
    pr_number_for_issue,
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


def test_advance_requirements_to_design() -> None:
    assert advance(Stage.REQUIREMENTS, StageSignals(has_refined_spec=True)) == (
        Stage.DESIGN,
        "requirements_refined",
    )
    # Kein Signal → bleibt requirements.
    assert advance(Stage.REQUIREMENTS, StageSignals()) == (Stage.REQUIREMENTS, "")


def test_advance_release_to_done() -> None:
    assert advance(Stage.RELEASE, StageSignals(release_done=True)) == (
        Stage.DONE,
        "released",
    )
    assert advance(Stage.RELEASE, StageSignals()) == (Stage.RELEASE, "")


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
    assert plan.dispatch == [DispatchOrder(1, Stage.IN_DEV)]
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
    assert plan.dispatch == [DispatchOrder(2, Stage.IN_DEV)]


def test_plan_tick_capacity_limits_dispatch() -> None:
    items = [_wi(n, Stage.READY) for n in (1, 2, 3)]
    plan = plan_tick(items, capacity=1)
    # nach Nummer sortiert, nur 1 Slot
    assert plan.dispatch == [DispatchOrder(1, Stage.IN_DEV)]


def test_plan_tick_dispatches_design_in_place_without_plan() -> None:
    # design ohne Plan → in-place dispatcht (Team architect), KEIN Stage-Wechsel.
    plan = plan_tick([_wi(1, Stage.DESIGN)], capacity=5)
    assert plan.dispatch == [DispatchOrder(1, Stage.DESIGN)]
    assert plan.transitions == []  # bleibt design, bis ein Plan vorliegt


def test_plan_tick_design_with_plan_advances_not_dispatched() -> None:
    # design + Plan → advance design→ready, NICHT im selben Tick dispatcht.
    plan = plan_tick(
        [_wi(1, Stage.DESIGN, signals=StageSignals(has_plan=True))], capacity=5
    )
    assert plan.dispatch == []
    assert plan.transitions == [
        StageTransition(1, Stage.DESIGN, Stage.READY, "plan_proposed")
    ]


def test_plan_tick_dispatches_qa_in_place_for_open_pr() -> None:
    # qa mit offenem PR, noch kein Review → in-place dispatcht (Review-Merge-Team).
    plan = plan_tick(
        [_wi(1, Stage.QA, signals=StageSignals(has_open_pr=True))], capacity=5
    )
    assert plan.dispatch == [DispatchOrder(1, Stage.QA)]
    assert plan.transitions == []  # bleibt qa, bis gemergt


def test_plan_tick_qa_review_done_not_redispatched() -> None:
    # qa, Review schon gelaufen (request_changes, nicht gemergt) → kein Re-Review.
    plan = plan_tick(
        [_wi(1, Stage.QA, signals=StageSignals(has_open_pr=True, review_done=True))],
        capacity=5,
    )
    assert plan.dispatch == []
    assert plan.transitions == []


def test_plan_tick_qa_merged_advances_to_release() -> None:
    # qa + gemergt → advance qa→release, NICHT im selben Tick re-dispatcht.
    plan = plan_tick(
        [_wi(1, Stage.QA, signals=StageSignals(has_merged_pr=True, review_done=True))],
        capacity=5,
    )
    assert plan.dispatch == []
    assert plan.transitions == [
        StageTransition(1, Stage.QA, Stage.RELEASE, "pr_merged")
    ]


# --- B1: requirements → design --------------------------------------------


def test_plan_tick_requirements_in_place_dispatch() -> None:
    # requirements ohne refined spec → in-place dispatch (Team = analyst/architect),
    # kein Stage-Wechsel.
    plan = plan_tick([_wi(1, Stage.REQUIREMENTS)], capacity=1)
    assert plan.dispatch == [DispatchOrder(1, Stage.REQUIREMENTS)]
    assert plan.transitions == []


def test_plan_tick_requirements_advances_when_refined() -> None:
    plan = plan_tick(
        [_wi(1, Stage.REQUIREMENTS, signals=StageSignals(has_refined_spec=True))],
        capacity=1,
    )
    # advance requirements→design, NICHT im selben Tick re-dispatcht.
    assert plan.dispatch == []
    assert plan.transitions == [
        StageTransition(1, Stage.REQUIREMENTS, Stage.DESIGN, "requirements_refined")
    ]


# --- B2: release → done ----------------------------------------------------


def test_plan_tick_release_in_place_dispatch() -> None:
    # release ohne release_done → in-place dispatch (deterministischer Effekt).
    plan = plan_tick([_wi(1, Stage.RELEASE)], capacity=1)
    assert plan.dispatch == [DispatchOrder(1, Stage.RELEASE)]
    assert plan.transitions == []


def test_plan_tick_release_advances_to_done() -> None:
    plan = plan_tick(
        [_wi(1, Stage.RELEASE, signals=StageSignals(release_done=True))],
        capacity=1,
    )
    assert plan.dispatch == []
    assert plan.transitions == [
        StageTransition(1, Stage.RELEASE, Stage.DONE, "released")
    ]


# --- A1: in-dev Re-Dispatch / Eskalation ----------------------------------


def test_plan_tick_in_dev_redispatch_when_no_pr() -> None:
    # in-dev, Run ohne PR, Versuche < MAX → Re-Dispatch in-place.
    plan = plan_tick(
        [_wi(1, Stage.IN_DEV, signals=StageSignals(dev_failed_no_pr=True, dev_attempts=1))],
        capacity=1,
    )
    assert plan.dispatch == [DispatchOrder(1, Stage.IN_DEV)]
    # KEIN ready→in-dev-Übergang (Item steht schon auf in-dev).
    assert plan.transitions == []
    assert plan.blocked == []


def test_plan_tick_in_dev_escalates_after_max_retries() -> None:
    plan = plan_tick(
        [
            _wi(
                1,
                Stage.IN_DEV,
                signals=StageSignals(dev_failed_no_pr=True, dev_attempts=MAX_DEV_RETRIES),
            )
        ],
        capacity=1,
    )
    assert plan.dispatch == []
    assert plan.blocked == [
        Blocked(
            1,
            "dev_exhausted",
            (),
            f"dev run produced no PR after {MAX_DEV_RETRIES} attempts",
        )
    ]
    assert plan.transitions == [
        StageTransition(1, Stage.IN_DEV, Stage.BLOCKED, "dev_retries_exhausted")
    ]


def test_plan_tick_in_dev_without_failure_signal_not_dispatched() -> None:
    # in-dev ohne Failure-Signal → kein Dispatch (wartet auf PR/advance).
    plan = plan_tick([_wi(1, Stage.IN_DEV)], capacity=5)
    assert plan.dispatch == []
    assert plan.transitions == []
    assert plan.blocked == []


def test_plan_tick_design_blocked_on_unmet_deps() -> None:
    # design #2 hängt an #1 (nicht done) → blocked, kein Design-Run.
    items = [_wi(1, Stage.IN_DEV), _wi(2, Stage.DESIGN, deps=[1])]
    plan = plan_tick(items, capacity=5)
    assert plan.dispatch == []
    assert any(b.number == 2 and b.kind == "deps" for b in plan.blocked)


def test_plan_tick_shared_capacity_across_teams() -> None:
    # design #1 + ready #2 konkurrieren um 1 Slot → #1 (kleinere Nummer) gewinnt.
    items = [_wi(1, Stage.DESIGN), _wi(2, Stage.READY)]
    plan = plan_tick(items, capacity=1)
    assert plan.dispatch == [DispatchOrder(1, Stage.DESIGN)]


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
    dispatched: list[DispatchOrder] = []
    blocked: list[Blocked] = []

    def set_stage(t: StageTransition) -> None:
        order.append(f"stage:{t.number}:{t.to_stage.value}")
        transitions.append(t)

    def dispatch(o: DispatchOrder) -> None:
        order.append(f"dispatch:{o.number}")
        dispatched.append(o)

    res = run_conductor_tick(
        items=items,
        capacity=5,
        set_stage=set_stage,
        dispatch=dispatch,
        on_blocked=blocked.append,
    )
    assert res.dispatched == 1
    assert dispatched == [DispatchOrder(1, Stage.IN_DEV)]
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
    """Minimaler Event-Stand-in (kind/run_id/payload/ts)."""

    def __init__(self, kind, run_id, payload, ts=None):
        self.kind = kind
        self.run_id = run_id
        self.payload = payload
        self.ts = ts or datetime(2026, 1, 1, tzinfo=UTC)


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


def test_derive_signals_release_done_via_issue_number() -> None:
    from forge_core.events import EventKind as EK

    # ReleaseTagged hat keinen RunStarted → korreliert direkt über issue_number.
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 9}),
        _Evt(EK.RELEASE_TAGGED, "session-x", {"issue_number": 9, "tag": "forge-issue-9"}),
    ]
    assert derive_signals(events, 9).release_done is True
    # Anderes Issue schlägt nicht durch.
    assert derive_signals(events, 8).release_done is False


def test_derive_signals_has_refined_spec() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 9}),
        _Evt(
            EK.REQUIREMENTS_REFINED,
            "r1",
            {"issue_number": 9, "insufficient_context": False},
        ),
    ]
    assert derive_signals(events, 9).has_refined_spec is True
    # insufficient_context → kein Advance-Signal.
    events2 = [
        _Evt(EK.RUN_STARTED, "r2", {"issue_number": 9}),
        _Evt(
            EK.REQUIREMENTS_REFINED,
            "r2",
            {"issue_number": 9, "insufficient_context": True},
        ),
    ]
    assert derive_signals(events2, 9).has_refined_spec is False


def test_derive_signals_insufficient_context_is_no_plan() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 1}),
        _Evt(EK.PLAN_PROPOSED, "r1", {"insufficient_context": True}),
    ]
    assert derive_signals(events, 1).has_plan is False


def test_derive_signals_review_done_from_pr_reviewed() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
        _Evt(EK.PR_REVIEWED, "rX", {"pr_number": 100, "verdict": "request_changes"}),
    ]
    sig = derive_signals(events, 42)
    assert sig.has_open_pr is True
    assert sig.review_done is True
    assert sig.has_merged_pr is False


def test_derive_signals_review_stale_when_new_commit() -> None:
    """A2: ein Commit NACH dem Review → review_done False (Re-Review fällig)."""
    from forge_core.events import EventKind as EK

    review_ts = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
        _Evt(EK.PR_REVIEWED, "rX", {"pr_number": 100}, ts=review_ts),
    ]
    # Commit später als Review → veraltet.
    newer = datetime(2026, 6, 1, 13, 0, tzinfo=UTC)
    assert derive_signals(events, 42, head_committed_at=newer).review_done is False
    # Commit vor/gleich Review → noch gültig.
    older = datetime(2026, 6, 1, 11, 0, tzinfo=UTC)
    assert derive_signals(events, 42, head_committed_at=older).review_done is True
    assert derive_signals(events, 42, head_committed_at=review_ts).review_done is True
    # Ohne Board-Datum → Fallback aufs alte Verhalten (Review existiert).
    assert derive_signals(events, 42, head_committed_at=None).review_done is True


def test_derive_signals_review_done_uses_latest_review() -> None:
    """A2: bei mehreren Reviews zählt das jüngste, nicht das erste."""
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
        _Evt(EK.PR_REVIEWED, "rX", {"pr_number": 100},
             ts=datetime(2026, 6, 1, 10, 0, tzinfo=UTC)),
        _Evt(EK.PR_REVIEWED, "rY", {"pr_number": 100},
             ts=datetime(2026, 6, 1, 14, 0, tzinfo=UTC)),
    ]
    # Commit zwischen den beiden Reviews → das jüngste Review (14:00) ist neuer
    # → noch gültig (sonst würde ein altes Review jeden Commit veralten lassen).
    between = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    assert derive_signals(events, 42, head_committed_at=between).review_done is True


# --- derive_dev_failure (A1) ----------------------------------------------


def _t(hour: int):
    return datetime(2026, 1, 1, hour, tzinfo=UTC)


def test_derive_dev_failure_latest_fail_no_pr() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5, "focus": "bug"}, ts=_t(0)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "no_improvement"}, ts=_t(1)),
    ]
    assert derive_dev_failure(events, 5) == (True, 1)


def test_derive_dev_failure_rate_limited_excluded() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5}, ts=_t(0)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "rate_limited"}, ts=_t(1)),
    ]
    # rate_limited gehört dem Resume-Pfad → kein Dev-Failure.
    assert derive_dev_failure(events, 5) == (False, 0)


def test_derive_dev_failure_pr_present_not_failure() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5}, ts=_t(0)),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}, ts=_t(1)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "pr_created"}, ts=_t(1)),
    ]
    assert derive_dev_failure(events, 5) == (False, 0)


def test_derive_dev_failure_ignores_design_run() -> None:
    from forge_core.events import EventKind as EK

    # design-Run mit gleichem issue_number darf NICHT als Dev-Failure zählen.
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5, "focus": "design:#5"}, ts=_t(0)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "no_improvement"}, ts=_t(1)),
    ]
    assert derive_dev_failure(events, 5) == (False, 0)


def test_derive_dev_failure_in_flight_retry_suppressed() -> None:
    from forge_core.events import EventKind as EK

    # Ein neuer Dev-Run NACH dem letzten Finish läuft noch → nicht erneut flaggen.
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5}, ts=_t(0)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "error"}, ts=_t(1)),
        _Evt(EK.RUN_STARTED, "r2", {"issue_number": 5}, ts=_t(2)),
    ]
    failed, attempts = derive_dev_failure(events, 5)
    assert failed is False
    assert attempts == 1


def test_derive_dev_failure_counts_multiple_attempts() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 5}, ts=_t(0)),
        _Evt(EK.RUN_FINISHED, "r1", {"decision": "no_improvement"}, ts=_t(1)),
        _Evt(EK.RUN_STARTED, "r2", {"issue_number": 5}, ts=_t(2)),
        _Evt(EK.RUN_FINISHED, "r2", {"decision": "error"}, ts=_t(3)),
    ]
    assert derive_dev_failure(events, 5) == (True, 2)


def test_pr_number_for_issue_resolves_open_pr() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
    ]
    assert pr_number_for_issue(events, 42) == 100
    assert pr_number_for_issue(events, 99) is None


def test_pr_number_for_issue_excludes_merged() -> None:
    from forge_core.events import EventKind as EK

    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 42}),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 100}),
        _Evt(EK.PR_MERGED, "rX", {"pr_number": 100}),
    ]
    assert pr_number_for_issue(events, 42) is None
