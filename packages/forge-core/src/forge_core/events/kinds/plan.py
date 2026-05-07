"""PlanProposed payload.

Spec v0.3 Teil 4.3 / 6.1: der architect-Subagent produziert pro Generation
einen Plan als Markdown. forge persistiert ihn als CAS-Blob (artifacts.plan)
und emittiert dieses Event mit Best-effort-extrahierten Metadaten.

Plan-Text selbst liegt im Blob-Store, hier nur Metadaten + Statistiken.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

RiskLevel = Literal["low", "medium", "high", "unknown"]


class PlanProposedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    architect_turns: int = 0
    """Anzahl claude-Turns im architect-Subagent-Aufruf."""

    subtask_count: int | None = None
    """Wenn der Plan parsbar war: Anzahl numerierter Subtasks. Sonst None."""

    risk_level: RiskLevel = "unknown"
    """Aus dem ## Risk-Header des Plans extrahiert (low/medium/high/unknown)."""

    out_of_scope: list[str] = Field(default_factory=list)
    """Bullets aus der ## Out of scope-Sektion, falls vorhanden."""

    insufficient_context: bool = False
    """True, wenn der architect 'Insufficient context' meldete statt eines
    Plans. In diesem Fall wird die Generation als plan_unclear beendet."""


register_payload(EventKind.PLAN_PROPOSED, PlanProposedPayload, "1.0")
