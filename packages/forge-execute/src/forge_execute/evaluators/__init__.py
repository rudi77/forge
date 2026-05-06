"""Evaluator-Implementierungen.

In v1 nur `CommandEvaluator` — führt Shell-Commands aus den `eval_suites`
der Spec aus und parst das Ergebnis (Spec Teil 6.4).
"""

from forge_execute.evaluators.command import (
    CommandEvaluator,
    EvalRunResult,
    parse_pytest_output,
    parse_scores_json,
)

__all__ = [
    "CommandEvaluator",
    "EvalRunResult",
    "parse_pytest_output",
    "parse_scores_json",
]
