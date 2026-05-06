"""PRCreated / PRMerged / PRReverted payloads.

`PRMerged`/`PRReverted` werden in v1 erst nach manueller Aktion am GitHub-PR
via Webhook emittiert. Auto-Merge gibt es nicht (Spec Teil 4.2, 7.5).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload


class PRCreatedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int
    branch: str
    base_branch: str = "main"
    labels: list[str] = Field(default_factory=list)
    url: str | None = None


class PRMergedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int
    merger: str
    """GitHub-Username der Person, die merged hat."""

    time_to_merge_s: int
    """Sekunden zwischen PR-Erzeugung und Merge."""


RevertReason = Literal[
    "explicit_revert_commit",
    "branch_hard_reset",
    "manual_signal",
]


class PRRevertedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int
    revert_reason: RevertReason
    original_pr: int
    """Die PR-Nummer, die revertet wurde — meist == pr_number, kann abweichen."""


register_payload(EventKind.PR_CREATED, PRCreatedPayload, "1.0")
register_payload(EventKind.PR_MERGED, PRMergedPayload, "1.0")
register_payload(EventKind.PR_REVERTED, PRRevertedPayload, "1.0")
