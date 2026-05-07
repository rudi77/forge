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
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol


class CodingAgentError(RuntimeError):
    """Generischer Fehler aus einem Agent-Aufruf."""


class CodingAgentTimeout(CodingAgentError):
    """Agent-Aufruf hat Budget oder Wallclock überschritten."""


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

    @property
    def has_changes(self) -> bool:
        return bool(self.diff.strip())


class CodingAgent(Protocol):
    """Was der Runner von einem Coding-Agent erwartet.

    Nur eine Methode in v1: `propose`. `review` und `estimate_cost` aus dem
    Spec-Entwurf sind v2/v3 — werden nachgezogen, sobald der Trigger
    `on_pr_opened` aktiv genutzt wird.
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
    ) -> ProposalResult:
        """Schickt einen Vorschlag an den Agent. Modifiziert den Worktree."""
        ...
