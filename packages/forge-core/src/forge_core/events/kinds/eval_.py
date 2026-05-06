"""EvalStarted / EvalFinished payloads.

`EvalFinished` enthält die strukturelle Trennung Gates/Scores/Diagnostics
(Spec Teil 5.1). `composite_value` wird nur gesetzt, wenn alle Gates grün sind.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

EvalMode = Literal["quick", "full", "judge"]


class EvalStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_mode: EvalMode
    suite_id: str
    budget_s: int


class GateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    passed: bool
    actual: float | None = None
    threshold: float | None = None
    reason: str | None = None


class EvalFinishedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_mode: EvalMode
    suite_id: str

    gates_passed: bool
    gates: list[GateResult] = Field(default_factory=list)

    scores: dict[str, float] = Field(default_factory=dict)
    composite_value: float | None = None
    """Nur gesetzt, wenn gates_passed == True."""

    diagnostics: dict[str, float] = Field(default_factory=dict)


register_payload(EventKind.EVAL_STARTED, EvalStartedPayload, "1.0")
register_payload(EventKind.EVAL_FINISHED, EvalFinishedPayload, "1.0")
