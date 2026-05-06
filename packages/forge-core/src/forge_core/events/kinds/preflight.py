"""PreflightFailed payload.

Preflight-Failure führt zu Discard ohne Eval-Run (Spec Teil 6.3).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload


class PreflightFailedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preflight_id: str
    """ID des Guardrail-Befehls aus surfaces.<name>.guardrails."""

    surface: str
    """Surface-Name, dessen Guardrail-Check fehlschlug."""

    command: str
    exit_code: int


register_payload(EventKind.PREFLIGHT_FAILED, PreflightFailedPayload, "1.0")
