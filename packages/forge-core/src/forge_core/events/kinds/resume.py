"""RunResumeScheduled payload.

Ein Run kann mitten in der Propose-Phase in ein Claude-Usage-/Session-Limit
laufen (HTTP 429, ``result``-Event mit ``is_error: true`` und Text
``"You've hit your session limit · resets <time>"``). Statt den Run als
``no_improvement`` mit verlorenem Cost zu verbuchen, hält forge einen
**Resume-Anker** fest: die claude-``session_id`` (für ``claude --resume``),
den eingefrorenen Worktree samt WIP-Commit und — best-effort — den Zeitpunkt,
zu dem das Limit zurückgesetzt wird.

Mantra 3: Dieses Event ist reine Telemetrie/Anker. Der Runner emittiert es,
**entscheidet** aber nicht, wann fortgesetzt wird — das tut Loop 2 (der
Conductor leitet fällige Resumes rein aus diesem Event-Strom ab).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload


class RunResumeScheduledPayload(BaseModel):
    """Anker, um einen vom Usage-Limit unterbrochenen Run fortzusetzen."""

    model_config = ConfigDict(extra="forbid")

    original_run_id: str
    """run_id des unterbrochenen Runs — dieselbe Linie wird beim Resume fortgesetzt."""

    resume_session_id: str | None = None
    """claude-``session_id`` aus dem stream-json (init-/result-Event). ``None``,
    wenn sie nicht extrahiert werden konnte — dann ist nur ein Worktree-basiertes
    Continue möglich, kein echtes claude-Session-Resume."""

    resume_at: datetime | None = None
    """UTC-Zeitpunkt, zu dem das Limit laut Agent-Text zurückgesetzt wird
    (best-effort geparst aus ``"resets 1pm (Europe/Vienna)"``). ``None``, wenn
    nicht parsebar — dann muss der Operator den Resume manuell auslösen."""

    resume_worktree: str
    """Absoluter Pfad zum eingefrorenen Worktree, in dem die Partial-Arbeit liegt."""

    wip_commit: str | None = None
    """SHA des ``forge:``-WIP-Commits, der den Partial-Diff sichert. ``None``,
    wenn nichts zu committen war (kein Diff zum Limit-Zeitpunkt)."""

    reason: str = "session_limit"
    """Warum der Resume geplant wurde — v1 immer ``"session_limit"`` (429)."""

    issue_number: int | None = None
    """Issue, das den Run ausgelöst hat — Korrelations-Schlüssel für Loop 2."""


register_payload(EventKind.RUN_RESUME_SCHEDULED, RunResumeScheduledPayload, "1.0")
