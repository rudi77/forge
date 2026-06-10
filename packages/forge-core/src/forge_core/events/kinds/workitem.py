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

BlockedKind = Literal["deps", "cycle", "error", "stalled"]


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
    Dependency-Graph, ``error`` = Dispatch/Adapter-Fehler, ``stalled`` =
    der jüngste Run des Items endete, ohne sein Stage-Artefakt (PR bzw.
    Plan) zu produzieren — Operator-Eskalation statt stillem Endlos-Retry."""

    blocked_by: list[int] = Field(default_factory=list)
    """Issue-Nummern, die das Item blockieren (bei ``deps``/``cycle``)."""

    reason: str = Field(default="", max_length=500)


register_payload(
    EventKind.WORK_ITEM_STAGE_CHANGED, WorkItemStageChangedPayload, "1.0"
)
# 1.1: additiv — BlockedKind um "stalled" erweitert (alte Events lesen weiter).
register_payload(EventKind.WORK_ITEM_BLOCKED, WorkItemBlockedPayload, "1.1")
