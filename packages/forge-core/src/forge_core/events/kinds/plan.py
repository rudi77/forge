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

# Status eines einzelnen Subtask-Schritts. `unknown` ist der Default, wenn
# der Orchestrator keinen expliziten Abschluss-Status für den Schritt
# zurückgemeldet hat (best-effort — der Plan-Text bleibt autoritativ im Blob).
SubtaskStatus = Literal["done", "open", "failed", "unknown"]


class PlanSubtask(BaseModel):
    """Ein einzelner geplanter Schritt aus der ``## Subtasks``-Sektion.

    Macht die Aufträge der Arbeitspferde (architect/developer/tester) im
    Event sichtbar, ohne den vollen Plan-Text zu duplizieren — der bleibt
    als CAS-Blob unter ``artifacts.plan``.
    """

    model_config = ConfigDict(extra="forbid")

    index: int
    """1-basierter Index des Subtasks, wie im Plan numeriert."""

    title: str
    """Kurztitel des Subtasks (erste fettgedruckte Phrase bzw. erste Zeile)."""

    status: SubtaskStatus = "unknown"
    """Best-effort-Abschlussstatus, falls der Orchestrator ihn zurückmeldet."""


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

    # --- v1.1 (additiv) -------------------------------------------------

    subtasks: list[PlanSubtask] = Field(default_factory=list)
    """Strukturierte Liste der geplanten Schritte. Leer bei alten Events
    (Schema 1.0) oder wenn der Plan keine ## Subtasks-Sektion hatte."""

    agents_used: list[str] = Field(default_factory=list)
    """Welche Arbeitspferde-Rollen am Run mitwirkten (z.B.
    ['architect', 'developer', 'tester']). Aus der aktivierten
    ``agents:[...]``-Roster der Trigger-Config abgeleitet. Leer bei alten
    Events oder Single-Agent-Runs ohne Roster."""


# Schema 1.1: additive Felder `subtasks` + `agents_used` (Spec-Regel:
# additive Änderung → Minor-Bump, alte Events lesen weiter, weil beide
# Felder Defaults haben).
register_payload(EventKind.PLAN_PROPOSED, PlanProposedPayload, "1.1")
