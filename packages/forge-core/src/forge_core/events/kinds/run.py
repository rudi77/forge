"""RunStarted / RunFinished payloads."""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

TriggerKind = Literal[
    "issue_label",
    "pr_opened",
    "ci_failure",
    "schedule",
    "manual",
]


class RunStartedPayload(BaseModel):
    """Loop-Start. In v1 identisch mit Loop-1-Start."""

    model_config = ConfigDict(extra="forbid")

    trigger: TriggerKind
    strategy: Literal["sequential", "review_only"]
    config_hash: str
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    focus: str | None = None
    issue_number: int | None = None
    pr_number: int | None = None
    max_iterations: int | None = None
    extras: dict[str, Any] = Field(default_factory=dict)


RunDecision = Literal[
    "pr_created",
    "no_improvement",
    "cost_cap_hit",
    "preflight_blocked",
    "guardrail_blocked",
    "error",
]


class RunFinishedPayload(BaseModel):
    """Loop-Ende."""

    model_config = ConfigDict(extra="forbid")

    decision: RunDecision
    generations_count: int
    final_score: float | None = None
    baseline_score: float | None = None
    score_delta: float | None = None
    total_cost_usd: Decimal = Decimal("0")
    pr_number: int | None = None


register_payload(EventKind.RUN_STARTED, RunStartedPayload, "1.0")
register_payload(EventKind.RUN_FINISHED, RunFinishedPayload, "1.0")
