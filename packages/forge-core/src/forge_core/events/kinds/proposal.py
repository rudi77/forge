"""ProposalRequested / ProposalReceived payloads.

Inhalt selbst — Prompt-Text, Diff, Context — liegt nicht im Event, sondern im
Blob-Store. Im Event nur die `artifacts: {prompt: sha256:..., context_summary: sha256:...}`-
References (Spec Teil 4.1).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

ProposalStopReason = Literal[
    "end_turn",
    "max_tokens",
    "max_turns",
    "stop_sequence",
    "tool_use",
    "error",
    "timeout",
    "unknown",
]


class ProposalRequestedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_template_id: str
    context_artifact_keys: list[str] = Field(default_factory=list)
    """Logische Schlüssel der referenzierten Context-Artefakte (z.B. ["spec", "history"])."""

    max_turns: int | None = None
    requested_model: str | None = None


class ProposalReceivedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stop_reason: ProposalStopReason
    turns_used: int = 0
    files_touched: list[str] = Field(default_factory=list)


register_payload(EventKind.PROPOSAL_REQUESTED, ProposalRequestedPayload, "1.0")
register_payload(EventKind.PROPOSAL_RECEIVED, ProposalReceivedPayload, "1.0")
