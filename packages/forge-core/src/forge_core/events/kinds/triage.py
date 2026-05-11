"""IssueTriaged payload.

Spec v0.4 Teil 6.3: Pre-Phase im ``forge board-loop``. Vor dem teuren
Worktree+Plan+Implement-Zyklus wird jedes Issue gegen den aktuellen
``main``-Stand und die Liste offener PRs/Issues geprüft. Erkennt der
Triager das Issue als ``stale`` / ``duplicate`` / ``already_solved``,
überspringt die Loop das Issue (optional mit Kommentar + Close).

Triage ist LLM-gestützt — "veraltet" und "Duplikat" sind inhaltliche
Aussagen, kein Pattern-Match. Dafür ist der Output strikt typisiert,
damit Replay und ``forge analyze`` die Entscheidungen aggregieren können.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload

TriageDecision = Literal["relevant", "stale", "duplicate", "already_solved"]


class IssueTriagedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(gt=0)
    decision: TriageDecision
    reason: str = Field(default="", max_length=2000)
    """Kurze Begründung — wird als Issue-Kommentar gespiegelt, wenn
    ``triage.auto_comment`` aktiv ist."""

    related_pr: int | None = None
    """Bei ``decision=duplicate``: PR-Nummer, die dasselbe löst."""

    related_commit: str | None = None
    """Bei ``decision=already_solved``: Commit-SHA, der den Fix enthält.
    Akzeptiert Full-SHA oder Short-SHA — wird nicht validiert, der
    Triager bekommt die Form aus ``git log``-Output."""

    turns_used: int = 0
    """Claude-Tool-Turns im Triage-Aufruf."""


register_payload(EventKind.ISSUE_TRIAGED, IssueTriagedPayload, "1.0")
