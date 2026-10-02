"""Work-Item-Events (Conductor / Loop 2).

Der Conductor fährt eine Stage-State-Machine auf Work-Items (= Issues):
requirements → design → ready → in-dev → qa → release → done. Jeder
Stage-Übergang und jede Blockade ist ein Event (Mantra 2), damit ``forge
analyze`` und der Replay das Fließband über die Zeit rekonstruieren können.

Diese Events stehen ÜBER den Run-Events: ihr ``run_id`` ist die Conductor-
Session-ULID, das Work-Item wird über ``issue_number`` im Payload
referenziert (kein Envelope-Umbau — siehe conductor-design.md §8).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

BlockedKind = Literal[
    "deps",
    "cycle",
    "error",
    "dev_exhausted",
    "rework_exhausted",
    "rework_no_change",
    "ci_fix_exhausted",
    "file_conflict",
    "merge_conflict",
]


class WorkItemStageChangedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(gt=0)
    from_stage: str
    """Vorherige Stage als Label-String (z.B. ``forge:design``). Leer, wenn
    das Item neu in die State-Machine eintritt."""

    to_stage: str
    """Neue Stage als Label-String (z.B. ``forge:ready``)."""

    reason: str = Field(default="", max_length=500)
    """Warum der Conductor den Übergang effektierte (z.B. ``plan_proposed``,
    ``pr_created``, ``pr_merged``, ``dispatched``)."""


class WorkItemBlockedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(gt=0)
    kind: BlockedKind
    """``deps`` = wartet auf offene Dependencies, ``cycle`` = Zyklus im
    Dependency-Graph, ``error`` = Dispatch/Adapter-Fehler, ``dev_exhausted`` =
    in-dev-Run produzierte nach mehreren Versuchen keinen PR (eskaliert).
    1.2: ``rework_exhausted`` = zu viele request_changes-Runden,
    ``rework_no_change`` = Nacharbeits-Run hat nichts gepusht,
    ``ci_fix_exhausted`` = CI bleibt nach Fix-Versuchen rot,
    ``file_conflict`` = wartet auf ein Item mit überlappenden Dateien (G),
    ``merge_conflict`` = PR-Konflikt mit der Basis nicht auflösbar (G)."""

    blocked_by: list[int] = Field(default_factory=list)
    """Issue-Nummern, die das Item blockieren (bei ``deps``/``cycle``)."""

    reason: str = Field(default="", max_length=500)


register_payload(
    EventKind.WORK_ITEM_STAGE_CHANGED, WorkItemStageChangedPayload, "1.0"
)
# 1.1 (additiv): BlockedKind um "dev_exhausted" erweitert (in-dev-Eskalation
# nach erschöpften Re-Dispatch-Versuchen). Alte 1.0-Events lesen weiter, da der
# Wertebereich nur erweitert wurde.
# 1.2 (additiv): BlockedKind um rework_*/ci_fix_exhausted/file_conflict/
# merge_conflict erweitert (Roadmap L1/L2/G).
register_payload(EventKind.WORK_ITEM_BLOCKED, WorkItemBlockedPayload, "1.2")
