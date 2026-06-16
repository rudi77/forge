"""Stage-State-Machine (Conductor / Loop 2).

Ein Work-Item (= Issue) durchläuft ein Fließband, kodiert als ``forge:``-Label:

    requirements → design → ready → in-dev → qa → release → done

Dieses Modul ist **rein**: Stage-Enum, erlaubte Übergänge und die
Vorwärts-Ableitung ``advance(stage, signals)``. Die Signale (gibt es einen
Plan? einen offenen PR? einen gemergten PR?) werden vom Conductor aus dem
Event-Store gefüttert — hier wird nur entschieden, nichts gelesen.

Mantra 3: die State-Machine urteilt über Work-Items, nie über Loop-Logik.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Stage(StrEnum):
    REQUIREMENTS = "forge:requirements"
    DESIGN = "forge:design"
    READY = "forge:ready"
    IN_DEV = "forge:in-dev"
    QA = "forge:qa"
    RELEASE = "forge:release"
    DONE = "forge:done"
    BLOCKED = "forge:blocked"


# Kanonische Pipeline-Reihenfolge (ohne die Sonderzustände blocked/done-terminal).
PIPELINE: tuple[Stage, ...] = (
    Stage.REQUIREMENTS,
    Stage.DESIGN,
    Stage.READY,
    Stage.IN_DEV,
    Stage.QA,
    Stage.RELEASE,
    Stage.DONE,
)

# Erlaubte Übergänge. Der Conductor effektiert nur, was hier steht — jeder
# andere Wechsel ist ein Bug (oder ein manueller Operator-Eingriff, den der
# Conductor respektiert, aber nicht selbst auslöst). BLOCKED ist von jeder
# aktiven Stage aus erreichbar und wird nur manuell wieder verlassen.
ALLOWED_TRANSITIONS: dict[Stage, frozenset[Stage]] = {
    Stage.REQUIREMENTS: frozenset({Stage.DESIGN, Stage.BLOCKED}),
    Stage.DESIGN: frozenset({Stage.READY, Stage.BLOCKED}),
    Stage.READY: frozenset({Stage.IN_DEV, Stage.BLOCKED}),
    Stage.IN_DEV: frozenset({Stage.QA, Stage.READY, Stage.BLOCKED}),
    Stage.QA: frozenset({Stage.RELEASE, Stage.IN_DEV, Stage.BLOCKED}),
    Stage.RELEASE: frozenset({Stage.DONE, Stage.BLOCKED}),
    Stage.DONE: frozenset(),
    Stage.BLOCKED: frozenset(),
}


@dataclass(frozen=True)
class StageSignals:
    """Beobachtungen über ein Work-Item, aus dem Event-Store abgeleitet."""

    has_plan: bool = False
    """Ein ``PlanProposed`` (ohne ``insufficient_context``) liegt vor."""

    has_open_pr: bool = False
    """Für das Item wurde ein PR geöffnet (``PRCreated``)."""

    has_merged_pr: bool = False
    """Der PR des Items wurde gemergt (``PRMerged``)."""

    review_done: bool = False
    """Für den offenen PR liegt bereits ein ``PRReviewed`` vor (Agent hat
    geurteilt) UND seither kam kein neuer Commit. Gate gegen teures
    Endlos-Re-Review in der ``qa``-Stage: nach einem ``request_changes`` (kein
    Merge) wird NICHT erneut dispatcht, bis neue Commits den Review-Stand
    zurücksetzen (Loop 1c — die Wiring-Schicht injiziert dazu den
    Head-Commit-Zeitstempel in ``derive_signals``)."""

    dev_failed_no_pr: bool = False
    """Der jüngste *abgeschlossene* Dev-Run dieses Items endete ohne PR (kein
    ``pr_created``, nicht ``rate_limited``), es gibt aktuell keinen offenen PR
    und kein Dev-Run ist gerade in-flight. Auslöser für Re-Dispatch/Eskalation
    in der ``in-dev``-Stage (A1)."""

    dev_attempts: int = 0
    """Anzahl der bisher fehlgeschlagenen Dev-Runs (ohne PR) dieses Items. Aus
    dem Event-Strom abgeleitet (überlebt Heartbeat-Restarts). Erreicht sie
    ``MAX_DEV_RETRIES``, eskaliert der Conductor das Item nach ``blocked``
    statt erneut zu dispatchen."""


# Stages, in denen ein Team *in-place* arbeitet und dabei seinen
# Advance-Auslöser produziert (``design`` → architect-Team → ``PlanProposed`` →
# ``has_plan`` → ``design→ready``). Der Conductor dispatcht so ein Item, ohne es
# zu bewegen; ``advance`` schreibt es fort, sobald das Signal vorliegt. Das ist
# der „verschiedene-Teams"-Kern: jede Stage hier bekommt ihr eigenes Roster.
#
# ``qa`` arbeitet ebenfalls in-place: das Review-Merge-Team (``forge review-pr``)
# bewertet den offenen PR und merged ihn opt-in → ``PRMerged`` → ``has_merged_pr``
# → ``advance`` schreibt ``qa→release`` fort. Anders als ``design`` ist der
# QA-Dispatch durch ``signals.review_done`` gegated (s. ``StageSignals``), damit
# ein ``request_changes`` nicht jeden Tick ein teures Re-Review auslöst.
#
# ``in-dev`` steht bewusst NICHT hier: es wird beim ``ready→in-dev``-Übergang
# *gekoppelt* dispatcht (genau einmal). Produziert dieser Run jedoch keinen PR
# (``dev_failed_no_pr``), re-dispatcht ``plan_tick`` das Dev-Team bis
# ``MAX_DEV_RETRIES`` und eskaliert danach nach ``blocked`` (A1) — ein
# *beschränkter* Retry, kein stiller Endlos-Retry. Das ist ein eigener
# plan_tick-Zweig (nicht In-Place), weil er signal-gegated ist.
#
# Wächst die Liste (``requirements``/``release``), braucht jede neue Stage
# zusätzlich ein Advance-Signal in ``advance`` + ``StageSignals`` und einen
# Dispatch-Zweig in der board-loop-Wiring-Schicht.
IN_PLACE_WORK_STAGES: frozenset[Stage] = frozenset({Stage.DESIGN, Stage.QA})


def stage_of(labels: list[str]) -> Stage | None:
    """Die ``forge:``-Stage eines Issues aus seinen Labels, oder ``None``.

    Bei mehreren Stage-Labels (sollte nicht vorkommen) gewinnt die in der
    Pipeline am weitesten fortgeschrittene — defensiv gegen inkonsistente
    Label-Sets nach manuellen Eingriffen.
    """
    present = {lbl for lbl in labels if lbl in _STAGE_LABELS}
    if not present:
        return None
    if Stage.BLOCKED.value in present:
        return Stage.BLOCKED
    # Am weitesten fortgeschrittene Pipeline-Stage gewinnt.
    for stage in reversed(PIPELINE):
        if stage.value in present:
            return stage
    return None


def is_terminal(stage: Stage) -> bool:
    return stage in (Stage.DONE, Stage.BLOCKED)


def can_transition(frm: Stage, to: Stage) -> bool:
    return to in ALLOWED_TRANSITIONS.get(frm, frozenset())


def advance(stage: Stage, signals: StageSignals) -> tuple[Stage, str]:
    """Die nächste *automatische* Stage + Begründung, ohne Dependency-/
    Dispatch-Logik.

    Deckt nur die event-getriebenen Übergänge ab:
      - design  → ready    sobald ein Plan vorliegt
      - in-dev  → qa       sobald ein PR geöffnet wurde
      - qa      → release  sobald der PR gemergt wurde

    ``ready → in-dev`` passiert beim DISPATCH (Conductor, mit Kapazität +
    Dependencies) und ``release → done`` / ``requirements → design`` brauchen
    die devops- bzw. requirements-Rolle — beide hier bewusst NICHT automatisch.
    Gibt ``(stage, "")`` zurück, wenn kein Übergang fällig ist.
    """
    if stage == Stage.DESIGN and signals.has_plan:
        return Stage.READY, "plan_proposed"
    if stage == Stage.IN_DEV and signals.has_open_pr:
        return Stage.QA, "pr_created"
    if stage == Stage.QA and signals.has_merged_pr:
        return Stage.RELEASE, "pr_merged"
    return stage, ""


_STAGE_LABELS: frozenset[str] = frozenset(s.value for s in Stage)
