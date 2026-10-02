"""WorkItemCreated payload (Roadmap A4 — forge erzeugt Arbeit).

forge legt ein Work-Item (Issue / Azure Work Item) selbst an: aus einer
Epic-Zerlegung, einem Fund während eines Runs, einer Review-Folgeaufgabe, rotem
CI auf ``main`` oder einem Schedule. Nicht aus anderen Events ableitbar (die
Anlage ist ein Effekt beim Tracker) → eigenes Event.

Aus ``parent`` + ``depends_on`` ist der Arbeitsgraph eines Epics rekonstruierbar
(Abschnitt G); ``fingerprint`` macht die Anlage idempotent (gleicher Fund →
kein zweites Item); ``source`` macht die Annahmequote pro Quelle messbar
(``factory_intake``-View, Mantra 1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

WorkItemSource = Literal[
    "epic_decomposition",
    "run_finding",
    "review_followup",
    "ci_main_red",
    "schedule",
    "post_release",
]


class WorkItemCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(gt=0)
    """Nummer/ID des angelegten Items beim Tracker."""

    kind: Literal["epic", "feature", "story", "bug", "task", "spec"]
    title: str = Field(max_length=500)
    source: WorkItemSource
    stage: str
    """Start-Stage-Label (``forge:proposed`` = wartet auf Freigabe)."""

    parent: int | None = None
    depends_on: list[int] = Field(default_factory=list)
    expected_files: list[str] = Field(default_factory=list)
    """``Touches:``-Globs — Grundlage des Konflikt-Schedulings (G)."""

    fingerprint: str
    """``sha256:<hex>`` über Quelle + normalisierten Fund (Dedupe)."""

    origin_issue: int | None = None
    """Work-Item, in dessen Kontext der Fund entstand."""

    provider: str | None = None


register_payload(EventKind.WORK_ITEM_CREATED, WorkItemCreatedPayload, "1.0")
