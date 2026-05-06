"""GenerationStarted / GenerationFinished payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload


class GenerationStartedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_idx: int
    parent_score: float | None = None
    focus: str | None = None


GenerationDecision = Literal["keep", "discard"]


class GenerationFinishedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation_idx: int
    decision: GenerationDecision
    new_score: float | None = None
    score_delta: float | None = None
    reason: str


register_payload(EventKind.GENERATION_STARTED, GenerationStartedPayload, "1.0")
register_payload(EventKind.GENERATION_FINISHED, GenerationFinishedPayload, "1.0")
