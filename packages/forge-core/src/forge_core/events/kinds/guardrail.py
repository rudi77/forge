"""GuardrailViolation payload.

Wird emittiert, sobald die Maschine eine Forbidden Zone oder eine deaktivierte
Capability anfasst. Ist immer ein Recoverable-No: Run wird sofort beendet
(Spec Teil 6.2, 7.1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload

GuardrailKind = Literal[
    "forbidden_path",
    "surface_violation",
    "capability_denied",
    "egress_denied",
    "prompt_injection_suspected",
]


class GuardrailViolationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    guardrail_id: GuardrailKind
    attempted_action: str
    """Kurze Beschreibung dessen, was verhindert wurde — z.B. 'edit backend/src/routes/auth.py'."""

    blocked: bool = True
    """Immer True in v1 — die Maschine kann Guardrails nicht umgehen."""

    detail: str | None = None


register_payload(EventKind.GUARDRAIL_VIOLATION, GuardrailViolationPayload, "1.0")
