"""PRCreated / PRReviewed / PRMerged / PRReverted payloads.

`PRMerged`/`PRReverted` werden klassisch nach Aktion am GitHub-PR via
Webhook emittiert. Seit dem Agent-Review-Merge (forge merged opt-in selbst)
emittiert forge `PRReviewed` (Agent-Urteil über einen offenen PR) und kann
`PRMerged` auch selbst schreiben — siehe `merged_by_forge`.
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


PRReviewVerdict = Literal["approve", "request_changes"]
PRCiStatus = Literal["pass", "fail", "pending", "none", "unknown"]


class PRReviewedPayload(BaseModel):
    """Ergebnis eines Agent-Reviews über einen *offenen* PR.

    Im Gegensatz zum in-Worktree-``reviewer`` (vor PR) und zum gescorten
    ``Judge`` (in-Loop) bewertet dieser Schritt einen bereits geöffneten PR
    auf GitHub. forge sammelt PR-Diff + CI-Status, der Agent urteilt
    (``agent.review`` → ``score``/``verdict``), forge effektiert das
    GitHub-Review und — bei ``approve`` + grünem CI + aktivierter
    ``merge_pr``-Capability — den Merge. Mantra 3: der Agent urteilt, forge
    entscheidet/effektiert deterministisch.
    """

    model_config = ConfigDict(extra="forbid")

    pr_number: int
    verdict: PRReviewVerdict
    score: float
    """LLM-``judge_score`` ∈ [0, 1] des Review-Agents (fail-closed: 0.0)."""

    ci_status: PRCiStatus
    """Aggregierter CI-Status aus dem GitHub ``statusCheckRollup``."""

    merged: bool
    """Ob forge nach dem Review tatsächlich gemergt hat."""

    review_posted: bool = True
    """Ob das GitHub-Review (approve/request-changes) gepostet wurde."""

    merge_blocked_reason: str | None = None
    """Falls ``merged`` False ist obwohl approve: warum (z.B. ci_not_green,
    capability_disabled, below_threshold)."""


class PRMergedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_number: int
    merger: str
    """GitHub-Username der Person/des Bots, der/die gemergt hat. Bei
    forge-initiiertem Merge der authentifizierte gh-Login (Fallback
    ``"forge"``)."""

    time_to_merge_s: int
    """Sekunden zwischen PR-Erzeugung und Merge."""

    merged_by_forge: bool = False
    """True, wenn forge den Merge selbst angestoßen hat (Agent-Review-Merge),
    statt eines Webhook-Signals nach manuellem/GitHub-Merge. Additiv (1.1)."""

    merge_method: str | None = None
    """``squash`` | ``merge`` | ``rebase`` bei forge-initiiertem Merge. Additiv."""


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
register_payload(EventKind.PR_REVIEWED, PRReviewedPayload, "1.0")
# 1.1: additive Felder merged_by_forge + merge_method (Agent-Review-Merge).
# Alte 1.0-Events lesen weiter, weil beide Defaults haben.
register_payload(EventKind.PR_MERGED, PRMergedPayload, "1.1")
register_payload(EventKind.PR_REVERTED, PRRevertedPayload, "1.0")
