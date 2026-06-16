"""RequirementsRefined payload (Pipeline-Ende vorne / Conductor-Stage requirements).

Ein requirements-Run (Team = architect, ``create_pr=False``) verdichtet ein
rohes Issue zu testbaren Akzeptanzkriterien — analog zum design-Run, der einen
Plan produziert. Der Runner emittiert dieses Event statt ``PlanProposed``, wenn
``prompt_template_id == "requirements"``, damit ``derive_signals`` daraus
``has_refined_spec`` ableitet und der Conductor das Item ``requirements→design``
fortschreibt. Bei ``insufficient_context=True`` bleibt das Item in
``requirements`` (kein Advance) — dieselbe Konvention wie beim Plan.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload


class RequirementsRefinedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(gt=0)
    criteria_count: int | None = None
    """Anzahl der verdichteten Akzeptanzkriterien (best-effort aus dem
    Marker-Block geparst), oder ``None``."""

    insufficient_context: bool = False
    """Der Run konnte das Issue nicht verdichten (zu vage) → kein Advance."""

    analyst_turns: int = 0
    agents_used: list[str] = Field(default_factory=list)


register_payload(EventKind.REQUIREMENTS_REFINED, RequirementsRefinedPayload, "1.0")
