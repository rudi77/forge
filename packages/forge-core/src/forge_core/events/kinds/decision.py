"""DecisionMade payload.

Keep/Discard-Logik aus Spec Teil 6.5 — deterministisch, score-basiert.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload

DecisionReason = Literal[
    "improvement",
    "no_significant_change",
    "regression",
    "gate_failure",
    "preflight_failure",
    "guardrail_violation",
    "error",
]


class DecisionMadePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: bool
    reason: DecisionReason
    score_delta: float | None = None
    tolerance: float | None = None


register_payload(EventKind.DECISION_MADE, DecisionMadePayload, "1.0")
