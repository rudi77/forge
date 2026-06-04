"""Evaluator-Implementierungen.

- `CommandEvaluator` — führt Shell-Commands aus den `eval_suites` der
  Spec aus und parst das Ergebnis (Spec Teil 6.4).
- `JudgeEvaluator` — LLM-Judge-Verifikation gegen Akzeptanzkriterien,
  fail-closed (Spec v0.5).
"""

from forge_execute.evaluators.command import (
    CommandEvaluator,
    EvalRunResult,
    parse_pytest_output,
    parse_scores_json,
)
from forge_execute.evaluators.judge import (
    JUDGE_MEASUREMENT_KEY,
    JudgeEvaluator,
    JudgeOutcome,
)

__all__ = [
    "JUDGE_MEASUREMENT_KEY",
    "CommandEvaluator",
    "EvalRunResult",
    "JudgeEvaluator",
    "JudgeOutcome",
    "parse_pytest_output",
    "parse_scores_json",
]
