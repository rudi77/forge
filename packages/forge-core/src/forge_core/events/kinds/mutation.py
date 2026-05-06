"""MutationApplied payload."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

MutatorKind = Literal["code", "yaml-keys", "text"]


class MutationAppliedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mutator: MutatorKind
    files_changed: list[str] = Field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0


register_payload(EventKind.MUTATION_APPLIED, MutationAppliedPayload, "1.0")
