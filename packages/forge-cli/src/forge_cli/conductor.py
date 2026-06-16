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
from datetime import UTC, datetime

from forge_core.events import EventKind

from forge_cli.dependencies import find_cycle, unmet_dependencies
from forge_cli.stages import IN_PLACE_WORK_STAGES, Stage, StageSignals, advance

# Beschränkter Re-Dispatch eines in-dev-Items, dessen Run keinen PR produzierte,
# bevor der Conductor nach ``blocked`` eskaliert (A1). Bewusst eine
# Modul-Konstante statt eines Spec-Felds in v1 (Blast-Radius klein halten — die
# harte Ressourcen-Grenze sind Cost-Caps + max_turns auf der Loop-1-Seite). Wird
# das konfigurierbar, wandert es als optionales Feld in die Conductor-Config.
MAX_DEV_RETRIES: int = 2

# Decisions eines Dev-Runs, die als "kein PR, fehlgeschlagen" zählen. NICHT
# enthalten: ``pr_created`` (Erfolg) und ``rate_limited`` (gehört dem
# Resume-Pfad, ``derive_pending_resumes``).
_DEV_NO_PR_FAILURES: frozenset[str] = frozenset(
    {
        "no_improvement",
        "error",
        "cost_cap_hit",
        "guardrail_blocked",
        "preflight_blocked",
        "self_terminated",
    }
)


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
            # In-Place-Team arbeitet, kein Übergang (advance schreibt fort,
            # sobald das Auslöser-Signal vorliegt). QA ist zusätzlich durch
            # review_done gegated: ein bereits gereviewter (aber nicht
            # gemergter) PR wird nicht jeden Tick erneut reviewt.
            if w.stage == Stage.QA and w.signals.review_done:
                continue
            target = w.stage
        elif w.stage == Stage.IN_DEV and w.signals.dev_failed_no_pr:
            # Dev-Run produzierte keinen PR. Beschränkter Re-Dispatch (A1):
            # bis MAX_DEV_RETRIES erneut dispatchen, danach nach blocked
            # eskalieren — kein stiller Endlos-Retry.
            if w.signals.dev_attempts >= MAX_DEV_RETRIES:
                blocked.append(
                    Blocked(
                        w.number,
                        "dev_exhausted",
                        (),
                        f"dev run produced no PR after "
                        f"{w.signals.dev_attempts} attempts",
                    )
                )
                transitions.append(
                    StageTransition(
                        w.number,
                        Stage.IN_DEV,
                        Stage.BLOCKED,
                        "dev_retries_exhausted",
                    )
                )
                effective[w.number] = Stage.BLOCKED
                continue
            # Re-Dispatch in-place (kein Stage-Wechsel — Item ist schon in-dev).
            target = Stage.IN_DEV
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
    by_num = {w.number: w for w in items}
    selected = candidates[: max(0, capacity)]
    for order in selected:
        # Der ready→in-dev-Übergang gehört NUR zum Erst-Dispatch aus der
        # Warteschlange — NICHT zum A1-Re-Dispatch eines Items, das bereits auf
        # in-dev steht (sonst ein illegaler ready→in-dev-Übergang).
        if order.stage == Stage.IN_DEV and by_num[order.number].stage == Stage.READY:
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


def derive_signals(
    events: list,
    issue_number: int,
    *,
    head_committed_at: datetime | None = None,
) -> StageSignals:
    """Leitet die Stage-Signale eines Work-Items aus dem Event-Strom ab.

    Rein und replay-fähig: korreliert ``RunStarted.issue_number`` → ``run_id``
    und liest daraus Plan/PR/Merge-Signale. ``events`` ist eine Liste von
    ``Event``-Objekten (``.kind``, ``.run_id``, ``.payload``-dict).

      - has_plan      : ein PlanProposed (ohne insufficient_context) in einem
                        Run dieses Issues.
      - has_open_pr   : ein PRCreated in einem Run dieses Issues.
      - has_merged_pr : ein PRMerged, dessen pr_number zu einem PRCreated
                        dieses Issues gehört.
      - review_done   : der offene PR wurde bereits gereviewt UND seither kam
                        kein neuer Commit (siehe ``head_committed_at``).

    ``head_committed_at`` ist der Head-Commit-Zeitstempel des offenen PRs, von
    der Wiring-Schicht via Board-Query injiziert (Commit-ts steht NICHT im
    Event-Strom — so bleibt diese Funktion rein). Ist er **neuer** als das
    jüngste ``PRReviewed``, gilt der Review als veraltet → ``review_done=False``
    → das QA-Team reviewt den nachgebesserten ``request_changes``-PR erneut.
    ``None`` (kein Board-Datum / kein offener PR) → Fallback aufs alte
    Verhalten (``review_done`` = ein Review existiert), damit ein gh-Schluckauf
    keine Regression auslöst.
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
    review_tss = [
        e.ts
        for e in events
        if e.kind == EventKind.PR_REVIEWED
        and (e.payload or {}).get("pr_number") in pr_numbers
    ]
    latest_review_ts = max(review_tss) if review_tss else None
    # Veraltet, sobald ein Commit NACH dem jüngsten Review landete. ``<=`` →
    # ein Commit exakt zur Review-Zeit gilt als "nicht neuer" (bias zu weniger
    # teuren Re-Reviews). Ohne Board-Datum: altes Verhalten (Review existiert).
    review_done = latest_review_ts is not None and (
        head_committed_at is None or head_committed_at <= latest_review_ts
    )
    return StageSignals(
        has_plan=has_plan,
        has_open_pr=has_open_pr,
        has_merged_pr=has_merged_pr,
        review_done=review_done,
    )


def pr_number_for_issue(events: list, issue_number: int) -> int | None:
    """Die offene PR-Nummer eines Issues aus dem Event-Strom, oder ``None``.

    Korreliert ``RunStarted.issue_number`` → ``run_id`` → ``PRCreated.pr_number``
    (analog :func:`derive_signals`) und schließt bereits gemergte PRs aus.
    Wählt bei mehreren offenen PRs die höchste Nummer (jüngster PR). Die
    QA-Wiring-Schicht braucht das, um ``forge review-pr <N>`` zu dispatchen.
    """
    run_ids = {
        e.run_id
        for e in events
        if e.kind == EventKind.RUN_STARTED
        and (e.payload or {}).get("issue_number") == issue_number
    }
    if not run_ids:
        return None
    pr_numbers = {
        (e.payload or {}).get("pr_number")
        for e in events
        if e.kind == EventKind.PR_CREATED and e.run_id in run_ids
    }
    pr_numbers.discard(None)
    merged = {
        (e.payload or {}).get("pr_number")
        for e in events
        if e.kind == EventKind.PR_MERGED
    }
    open_prs = [n for n in pr_numbers if n not in merged]
    return max(open_prs) if open_prs else None


def _is_dev_run_started(payload: dict) -> bool:
    """True, wenn ein ``RunStarted``-Payload zu einem *Dev-Loop*-Run gehört
    (nicht design/requirements/review).

    Discriminator rein über vorhandene Felder: ein Dev-Run setzt kein
    ``pr_number`` (anders als Review) und hat keinen Stage-Präfix im ``focus``
    (design setzt ``design:#N``, requirements ``requirements:#N``).
    """
    if payload.get("pr_number") is not None:
        return False
    focus = payload.get("focus") or ""
    return not any(
        focus.startswith(prefix)
        for prefix in ("design:", "requirements:", "review:")
    )


def derive_dev_failure(events: list, issue_number: int) -> tuple[bool, int]:
    """Leitet ``(dev_failed_no_pr, dev_attempts)`` für ein ``in-dev``-Item ab.

    Rein und replay-fähig (Mantra 3). Ein Dev-Run zählt als „No-PR-Failure",
    wenn sein ``RunFinished`` eine Decision aus :data:`_DEV_NO_PR_FAILURES`
    trägt und kein ``PRCreated`` für seine ``run_id`` existiert.

      - ``dev_attempts``    : Anzahl solcher fehlgeschlagenen Dev-Runs.
      - ``dev_failed_no_pr``: der jüngste *abgeschlossene* Dev-Run ist ein
        No-PR-Failure UND es gibt aktuell keinen offenen PR (Sicherheits-Gate)
        UND kein Dev-Run ist gerade in-flight (ein ``RunStarted`` neuer als das
        jüngste ``RunFinished`` — verhindert Doppel-Dispatch, während ein Retry
        schon läuft; Vorbild: ``derive_pending_resumes``' already_resumed-Gate).
    """
    dev_started: dict[str, object] = {}
    for e in events:
        if (
            e.kind == EventKind.RUN_STARTED
            and (e.payload or {}).get("issue_number") == issue_number
            and _is_dev_run_started(e.payload or {})
        ):
            dev_started[e.run_id] = e.ts
    if not dev_started:
        return (False, 0)

    run_ids = set(dev_started)
    pr_run_ids = {
        e.run_id
        for e in events
        if e.kind == EventKind.PR_CREATED and e.run_id in run_ids
    }
    finishes = [
        e
        for e in events
        if e.kind == EventKind.RUN_FINISHED and e.run_id in run_ids
    ]
    finishes.sort(key=lambda e: e.ts)

    attempts = sum(
        1
        for e in finishes
        if (e.payload or {}).get("decision") in _DEV_NO_PR_FAILURES
        and e.run_id not in pr_run_ids
    )
    if not finishes:
        return (False, attempts)

    latest = finishes[-1]
    latest_is_failure = (
        (latest.payload or {}).get("decision") in _DEV_NO_PR_FAILURES
        and latest.run_id not in pr_run_ids
    )
    has_open_pr = pr_number_for_issue(events, issue_number) is not None
    in_flight = any(ts > latest.ts for ts in dev_started.values())
    failed_no_pr = latest_is_failure and not has_open_pr and not in_flight
    return (failed_no_pr, attempts)


@dataclass(frozen=True)
class ResumeOrder:
    """Ein fälliger Resume eines vom Usage-/Session-Limit unterbrochenen Runs.

    Vom Conductor rein aus dem Event-Strom abgeleitet; die Wiring-Schicht
    dispatcht ihn als ``forge run --resume <run_id>`` (gleiche Run-Linie).
    """

    run_id: str
    resume_session_id: str | None
    resume_at: datetime
    issue_number: int | None
    worktree: str


def _parse_iso(value: object) -> datetime | None:
    """Parst einen ISO-Timestamp aus einem Event-Payload (Store liefert str)."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        dt = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def derive_pending_resumes(events: list, now: datetime) -> list[ResumeOrder]:
    """Leitet **fällige** Run-Resumes rein aus dem Event-Strom ab (Mantra 3).

    Ein ``RunResumeScheduled`` ist fällig, wenn:

      1. seine ``resume_at`` gesetzt UND ``<= now`` ist (``None`` = Reset-Zeit war
         nicht parsebar → **nur manueller** Resume, nie auto-dispatcht), und
      2. der Run seither NICHT erneut gestartet wurde — d. h. es gibt kein
         ``RunStarted`` mit demselben ``run_id`` und ``ts`` nach dem Anker
         (at-most-once; ein Resume emittiert ein frisches RunStarted derselben
         run_id).

    Lief ein Run mehrfach ins Limit, zählt nur der **jüngste** Anker pro run_id.
    Der Conductor entscheidet das WANN über diese reine Ableitung — er greift
    nie in den Runner ein. ``events`` sind ``Event``-Objekte (``.kind``,
    ``.run_id``, ``.ts``, ``.payload``).
    """
    latest_anchor: dict[str, object] = {}
    for e in events:
        if e.kind == EventKind.RUN_RESUME_SCHEDULED:
            prev = latest_anchor.get(e.run_id)
            if prev is None or e.ts > prev.ts:  # type: ignore[attr-defined]
                latest_anchor[e.run_id] = e
    if not latest_anchor:
        return []

    orders: list[ResumeOrder] = []
    for run_id, anchor in latest_anchor.items():
        anchor_ts = anchor.ts  # type: ignore[attr-defined]
        already_resumed = any(
            e.kind == EventKind.RUN_STARTED
            and e.run_id == run_id
            and e.ts > anchor_ts
            for e in events
        )
        if already_resumed:
            continue
        payload = anchor.payload or {}  # type: ignore[attr-defined]
        resume_at = _parse_iso(payload.get("resume_at"))
        if resume_at is None or resume_at > now:
            continue  # nicht parsebar (manuell) oder noch nicht fällig
        orders.append(
            ResumeOrder(
                run_id=run_id,
                resume_session_id=payload.get("resume_session_id"),
                resume_at=resume_at,
                issue_number=payload.get("issue_number"),
                worktree=str(payload.get("resume_worktree", "")),
            )
        )
    return sorted(orders, key=lambda o: o.run_id)
