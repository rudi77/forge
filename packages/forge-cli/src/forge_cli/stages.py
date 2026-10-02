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
    # Roadmap A: Arbeit erzeugen. ``epic`` wird von einem Planner-Run in
    # Kind-Items zerlegt und wartet dann in ``tracking``, bis alle Kinder
    # ``done`` sind. ``proposed`` = von forge erzeugt, wartet auf Freigabe durch
    # einen Menschen — der Conductor verlässt diese Stage NIE selbst.
    EPIC = "forge:epic"
    TRACKING = "forge:tracking"
    PROPOSED = "forge:proposed"


# Marker-Labels, die forge zusätzlich zu den Stages setzt (keine Stage):
# ``forge:generated`` kennzeichnet von forge selbst erzeugte Work-Items (A3).
MARKER_LABELS: tuple[str, ...] = ("forge:generated",)


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
    Stage.EPIC: frozenset({Stage.TRACKING, Stage.BLOCKED}),
    Stage.TRACKING: frozenset({Stage.DONE, Stage.BLOCKED}),
    Stage.PROPOSED: frozenset(),
}


@dataclass(frozen=True)
class StageSignals:
    """Beobachtungen über ein Work-Item, aus dem Event-Store abgeleitet."""

    has_refined_spec: bool = False
    """Ein ``RequirementsRefined`` (ohne ``insufficient_context``) liegt vor —
    das requirements-Team hat testbare Akzeptanzkriterien verdichtet
    (Advance-Signal requirements→design)."""

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

    release_done: bool = False
    """Ein ``ReleaseTagged`` liegt vor — forge hat Tag + Release erzeugt
    (Advance-Signal release→done; opt-in ``capabilities.create_release``)."""

    changes_requested: bool = False
    """L1: das jüngste ``PRReviewed`` des offenen PRs ist ``request_changes``
    UND noch aktuell (kein Commit seit dem Review). Schickt das Item von ``qa``
    zurück nach ``in-dev`` und hält es dort, bis ein Nacharbeits-Run gepusht
    hat (dann ist der Review veraltet → ``in-dev → qa`` → frisches Review)."""

    rework_rounds: int = 0
    """L1: Anzahl ``request_changes``-Reviews über alle PRs des Items. Über
    ``MAX_REWORK_ROUNDS`` eskaliert der Conductor nach ``blocked``."""

    rework_started: bool = False
    """L1: seit dem jüngsten Review wurde bereits ein Nacharbeits-Run gestartet
    (in-flight oder fertig) → nicht erneut dispatchen."""

    rework_failed: bool = False
    """L1: ein Nacharbeits-Run seit dem jüngsten Review endete ohne Ergebnis
    (nichts gepusht) → eskalieren statt endlos neu zu versuchen."""

    ci_status: str | None = None
    """L2: CI-Status des offenen PRs (pass|fail|pending|none|unknown), von der
    Wiring-Schicht über den Code-Host injiziert (steht nicht im Event-Strom).
    ``None`` = unbekannt → altes Verhalten (kein CI-Gate)."""

    ci_fix_attempts: int = 0
    """L2: Anzahl CI-Fix-Runs (``trigger=ci_failure``) für das Item."""

    ci_fix_started: bool = False
    """L2: für den aktuellen PR-Head läuft bereits ein CI-Fix-Run / ist fertig."""

    ci_fix_failed: bool = False
    """L2: ein CI-Fix-Run für den aktuellen Head endete ohne Ergebnis."""

    spec_pending: bool = False
    """A1: für das Item ist ein Spec-PR (Label ``forge:spec``) offen und noch
    nicht gemergt → ``requirements`` wartet auf den Merge (menschliches Gate)."""

    has_decomposition: bool = False
    """A2: ein Epic wurde in Kind-Items zerlegt (``WorkItemCreated`` mit
    ``parent`` = Epic, Quelle ``epic_decomposition``) → ``epic → tracking``."""

    children_done: bool = False
    """A2: alle Kinder des Epics stehen auf ``done`` → ``tracking → done``."""

    epic_failed_runs: int = 0
    """A2: Zerlegungs-Runs, die keine Items erzeugten (Eskalation statt
    Endlos-Retry)."""

    @property
    def ci_failed(self) -> bool:
        return self.ci_status == "fail"


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
IN_PLACE_WORK_STAGES: frozenset[Stage] = frozenset(
    {Stage.REQUIREMENTS, Stage.DESIGN, Stage.QA, Stage.RELEASE, Stage.EPIC}
)


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
    # Am weitesten fortgeschrittene Pipeline-Stage gewinnt (ein Mensch, der
    # ein proposed-Item freigibt, setzt einfach forge:requirements dazu).
    for stage in reversed(PIPELINE):
        if stage.value in present:
            return stage
    for stage in (Stage.TRACKING, Stage.EPIC, Stage.PROPOSED):
        if stage.value in present:
            return stage
    return None


def is_terminal(stage: Stage) -> bool:
    return stage in (Stage.DONE, Stage.BLOCKED)


def can_transition(frm: Stage, to: Stage) -> bool:
    return to in ALLOWED_TRANSITIONS.get(frm, frozenset())


# L1: so viele request_changes-Runden darf ein Item durchlaufen, bevor der
# Conductor nach ``blocked`` eskaliert (Roadmap E3: erst Konstante).
MAX_REWORK_ROUNDS: int = 2

# L2: so viele CI-Fix-Runs pro Item, bevor der Conductor eskaliert.
MAX_CI_FIX_ATTEMPTS: int = 2


def advance(stage: Stage, signals: StageSignals) -> tuple[Stage, str]:
    """Die nächste *automatische* Stage + Begründung, ohne Dependency-/
    Dispatch-Logik.

    Deckt nur die event-getriebenen Übergänge ab:
      - requirements → design   sobald die Akzeptanzkriterien verdichtet sind
      - design       → ready     sobald ein Plan vorliegt
      - in-dev       → qa         sobald ein PR offen ist und kein aktuelles
                                  ``request_changes`` mehr gilt
      - qa           → release    sobald der PR gemergt wurde
      - qa           → in-dev     bei aktuellem ``request_changes`` (L1)
      - qa           → blocked    nach mehr als ``MAX_REWORK_ROUNDS`` Runden
      - qa           → in-dev     bei rotem CI (L2), ``blocked`` nach
                                  ``MAX_CI_FIX_ATTEMPTS`` Fix-Runs
      - release      → done        sobald Tag + Release erzeugt sind

    ``ready → in-dev`` passiert beim DISPATCH (Conductor, mit Kapazität +
    Dependencies) — hier bewusst NICHT automatisch. Gibt ``(stage, "")``
    zurück, wenn kein Übergang fällig ist.
    """
    if (
        stage == Stage.REQUIREMENTS
        and signals.has_refined_spec
        and not signals.spec_pending
    ):
        return Stage.DESIGN, "requirements_refined"
    if stage == Stage.EPIC and signals.has_decomposition:
        return Stage.TRACKING, "epic_decomposed"
    if stage == Stage.TRACKING and signals.children_done:
        return Stage.DONE, "children_done"
    if stage == Stage.DESIGN and signals.has_plan:
        return Stage.READY, "plan_proposed"
    if (
        stage == Stage.IN_DEV
        and signals.has_open_pr
        and not signals.changes_requested
        and not signals.ci_failed
    ):
        return Stage.QA, "pr_created"
    if stage == Stage.QA and signals.has_merged_pr:
        return Stage.RELEASE, "pr_merged"
    if stage == Stage.QA and signals.changes_requested:
        if signals.rework_rounds > MAX_REWORK_ROUNDS:
            return Stage.BLOCKED, "rework_exhausted"
        return Stage.IN_DEV, "review_changes_requested"
    if stage == Stage.QA and signals.ci_failed:
        if signals.ci_fix_attempts >= MAX_CI_FIX_ATTEMPTS:
            return Stage.BLOCKED, "ci_fix_exhausted"
        return Stage.IN_DEV, "ci_failed"
    if stage == Stage.RELEASE and signals.release_done:
        return Stage.DONE, "released"
    return stage, ""


_STAGE_LABELS: frozenset[str] = frozenset(s.value for s in Stage)
