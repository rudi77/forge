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
