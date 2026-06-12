"""CodingAgent-Protocol und gemeinsame Datentypen.

Designprinzip aus todos.txt: Claude Code CLI ist in forge ein Plug-in,
nicht das Fundament. Der Runner sieht nur dieses Protokoll. Morgen kann
ein `CodexCLIAgent`, `OpenCodeAgent` oder `DirectAnthropicAPIAgent` daneben
treten, ohne dass forge-execute strukturell anders aussieht.

Synchron in v1 — der Runner ist sequential, Subprozess-Aufrufe sind
sync. Async wird relevant in v2 (Population-Strategie).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol


class CodingAgentError(RuntimeError):
    """Generischer Fehler aus einem Agent-Aufruf."""


class CodingAgentTimeout(CodingAgentError):
    """Agent-Aufruf hat Budget oder Wallclock überschritten."""


class CodingAgentRateLimited(CodingAgentError):
    """Agent traf ein Claude-Usage-/Session-Limit (HTTP 429).

    Kein gewöhnlicher Fehler: der Worktree enthält i. d. R. Partial-Arbeit, und
    die claude-``session_id`` erlaubt ein späteres ``claude --resume``. Die
    Exception trägt den **Resume-Anker** (session_id, reset_at, echter Cost),
    damit der Runner ihn als ``RunResumeScheduled`` festhalten kann — statt den
    Run als ``no_improvement`` mit verlorenem Cost zu verbuchen.

    Subklasse von :class:`CodingAgentError`, damit bestehende
    ``except CodingAgentError`` weiter als Fallback greifen; der Runner fängt
    sie aber spezifischer ab.
    """

    def __init__(
        self,
        message: str,
        *,
        session_id: str | None,
        reset_at: datetime | None,
        cost_usd: Decimal,
        turns_used: int = 0,
        stream_log: str | None = None,
    ) -> None:
        super().__init__(message)
        self.session_id = session_id
        self.reset_at = reset_at
        self.cost_usd = cost_usd
        self.turns_used = turns_used
        self.stream_log = stream_log


@dataclass(frozen=True)
class ReviewResult:
    """Ergebnis eines ``review``-Aufrufs (LLM-Judge, Spec v0.5).

    Der Judge bewertet einen Diff gegen die Akzeptanzkriterien (= Issue-
    Text) und liefert einen kontinuierlichen ``judge_score`` ∈ [0, 1]
    plus eine binäre ``verdict``-Zusammenfassung und eine kurze
    Begründung. ``judge_score`` ist der Wert, der als ``llm_judge_score``
    in den Eval-Output gemerged wird; ``reasoning`` wird als Blob-
    Artefakt persistiert, damit der Replay nachvollziehbar bleibt.
    """

    judge_score: float
    verdict: Literal["pass", "fail"]
    reasoning: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")
    turns_used: int = 0
    duration_ms: int = 0
    model: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProposalResult:
    """Ergebnis eines `propose`-Aufrufs.

    Der eigentliche Diff wird NICHT vom Agent zurückgegeben, sondern vom
    Runner via `worktrees.diff_against_base()` ermittelt — der Agent editiert
    Files direkt im Worktree. Hier ist nur der Diff-Text mit aufgenommen,
    weil der Runner ihn dem Mutator und PR weiterreicht.

    `plan_md` ist der Plan-Markdown vom architect-Subagent (Spec v0.3 Teil 6.1)
    falls der Agent im Multi-Agent-Modus läuft und die Marker im finalen
    Output gefunden wurden. Sonst None — keine PlanProposed-Emission im Runner.
    """

    diff: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")
    stop_reason: str = "unknown"
    model: str | None = None
    model_version: str | None = None
    turns_used: int = 0
    duration_ms: int = 0
    raw_response: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    plan_md: str | None = None
    agents_invoked: list[str] | None = None
    """Subagent-Rollen, die der Master laut Selbstauskunft TATSÄCHLICH gerufen
    hat (aus dem `---FORGE-AGENTS-...---`-Block). None, wenn der Master nichts
    meldete oder Single-Agent-Run — dann fällt der Runner auf das konfigurierte
    Roster zurück. Macht Orchestrierungs-Treue messbar (config != reality)."""
    stream_log: str | None = None
    """Pfad zum stream-json-Log dieses propose-Aufrufs (ein Envelope
    ``{"ts", "event"}`` pro claude-Event). Lebt unter ``.forge/logs/<run_id>/``
    und überlebt DISCARD/Timeout — primäre Diagnosequelle, wenn ein Run lange
    braucht oder gekappt wird. None bei Agents ohne Streaming (Mock)."""
    session_id: str | None = None
    """claude-``session_id`` dieses Aufrufs (aus dem stream-json init-/result-
    Event). Resume-Anker für ``claude --resume``; None bei Agents ohne Streaming."""

    @property
    def has_changes(self) -> bool:
        return bool(self.diff.strip())


class CodingAgent(Protocol):
    """Was der Runner von einem Coding-Agent erwartet.

    Zwei Methoden: ``propose`` (modifiziert den Worktree) und ``review``
    (read-only Bewertung eines Diffs gegen Akzeptanzkriterien — der
    LLM-Judge, Spec v0.5). ``estimate_cost`` aus dem Spec-Entwurf bleibt
    v2/v3.
    """

    def propose(
        self,
        *,
        worktree: Path,
        prompt: str,
        max_turns: int,
        budget_usd: Decimal,
        model: str | None = None,
        allowed_tools: str | None = None,
        env: dict[str, str] | None = None,
        resume_session_id: str | None = None,
    ) -> ProposalResult:
        """Schickt einen Vorschlag an den Agent. Modifiziert den Worktree.

        ``resume_session_id`` setzt einen früheren, vom Usage-Limit
        unterbrochenen Lauf fort (agent-spezifisch, z. B. ``claude --resume``).
        Agents ohne Resume-Fähigkeit ignorieren den Parameter.
        """
        ...

    def review(
        self,
        *,
        worktree: Path,
        acceptance_criteria: str,
        diff: str,
        max_turns: int,
        budget_usd: Decimal,
        model: str | None = None,
        allowed_tools: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ReviewResult:
        """Bewertet den Diff gegen die Akzeptanzkriterien (read-only).

        Modifiziert den Worktree **nicht**. Bei Subprozess-Fehler oder
        unparsbarer Antwort wird ``CodingAgentError`` (bzw.
        ``CodingAgentTimeout``) geworfen — der Caller behandelt das
        fail-closed (Score 0.0 / fail).
        """
        ...
