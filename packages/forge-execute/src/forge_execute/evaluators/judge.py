"""Judge-Evaluator — LLM-gestützte Verifikation gegen Akzeptanzkriterien.

Dünne Schicht über ``CodingAgent.review``: baut den Judge-Aufruf, fängt
alle Fehler ab und übersetzt das Ergebnis in einen ``llm_judge_score``-
Messwert, der sich in den Eval-Output mergen lässt.

**Fail-closed** (Spec v0.5): scheitert der Judge (claude-Crash, Timeout,
JSON-Garbage), gilt ``score = 0.0`` / ``verdict = fail``. Damit bleibt
ein ``llm_judge_score``-Gate rot und die Änderung wird verworfen — nichts
wird ohne bestätigte Verifikation behalten. Der Judge darf nie ein KEEP
*erzwingen*, nur eines *erlauben*.

Der Judge mutiert den Worktree nicht; er bekommt ein read-only Tool-Set
über den Agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from forge_execute.agents.base import (
    CodingAgent,
    CodingAgentError,
    CodingAgentTimeout,
)

# Schlüssel, unter dem der Judge-Score in den Eval-Measurements landet.
# Muss mit dem GateKind/DiagnosticKind in forge_core.spec übereinstimmen.
JUDGE_MEASUREMENT_KEY = "llm_judge_score"


@dataclass(frozen=True)
class JudgeOutcome:
    """Ergebnis eines Judge-Laufs, runner-freundlich aufbereitet.

    ``score`` ist immer gesetzt (fail-closed: 0.0 bei Fehler). ``error``
    trägt die Fehlermeldung, falls der Judge-Aufruf scheiterte — der
    Runner persistiert sie zur Diagnose, behandelt das Ergebnis aber
    wie ein reguläres ``fail``.
    """

    score: float
    verdict: str
    reasoning: str
    cost_usd: Decimal = Decimal("0")
    turns_used: int = 0
    duration_ms: int = 0
    model: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    error: str | None = None

    @property
    def measurement(self) -> dict[str, float]:
        """Der Messwert, der in den Eval-Output gemerged wird."""
        return {JUDGE_MEASUREMENT_KEY: self.score}


class JudgeEvaluator:
    """Ruft ``agent.review`` auf und übersetzt das Ergebnis fail-closed.

    Verwendung::

        judge = JudgeEvaluator(agent)
        outcome = judge.run(
            worktree=wt.path,
            acceptance_criteria=issue_body,
            diff=current_diff,
            max_turns=6,
            budget_usd=Decimal("0.50"),
            model="claude-haiku-4-5",
        )
        measurements.update(outcome.measurement)
    """

    def __init__(self, agent: CodingAgent) -> None:
        self.agent = agent

    def run(
        self,
        *,
        worktree: Path,
        acceptance_criteria: str,
        diff: str,
        max_turns: int,
        budget_usd: Decimal,
        model: str | None = None,
    ) -> JudgeOutcome:
        try:
            review = self.agent.review(
                worktree=worktree,
                acceptance_criteria=acceptance_criteria,
                diff=diff,
                max_turns=max_turns,
                budget_usd=budget_usd,
                model=model,
            )
        except CodingAgentTimeout as exc:
            return JudgeOutcome(
                score=0.0,
                verdict="fail",
                reasoning=f"judge timeout (fail-closed): {exc}",
                error=str(exc),
            )
        except CodingAgentError as exc:
            return JudgeOutcome(
                score=0.0,
                verdict="fail",
                reasoning=f"judge error (fail-closed): {exc}",
                error=str(exc),
            )

        return JudgeOutcome(
            score=review.judge_score,
            verdict=review.verdict,
            reasoning=review.reasoning,
            cost_usd=review.cost_usd,
            turns_used=review.turns_used,
            duration_ms=review.duration_ms,
            model=review.model,
            tokens_in=review.tokens_in,
            tokens_out=review.tokens_out,
        )
