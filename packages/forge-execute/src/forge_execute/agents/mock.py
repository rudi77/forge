"""Mock-CodingAgent für Tests.

Erlaubt deterministische Antworten in Runner-Tests, ohne `claude` aufzurufen.
Drei Verwendungsmuster:

1. Statisch: ein vorgefertigter `ProposalResult` für jeden Aufruf.
2. Sequenz: eine Liste von `ProposalResult`s, einer pro Aufruf.
3. Callable: eine Funktion `(worktree, prompt) -> ProposalResult`, die
   den Worktree direkt manipuliert (z.B. ein File schreibt) und dann
   einen Result mit dem berechneten Diff zurückgibt.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

from forge_execute.agents.base import CodingAgent, ProposalResult, ReviewResult

_AgentFn = Callable[[Path, str], "ProposalResult | str | None"]
_ReviewFn = Callable[[Path, str, str], ReviewResult]


class MockCodingAgent:
    """Synchroner Mock-Agent.

    Verwendung — Modus 1 (statisch)::

        agent = MockCodingAgent(static_result=ProposalResult(diff="..."))

    Modus 2 (Sequenz)::

        agent = MockCodingAgent(sequence=[result1, result2])

    Modus 3 (Callable)::

        def fix(wt: Path, prompt: str) -> ProposalResult:
            (wt / "src" / "calc.py").write_text("...", encoding="utf-8")
            return ProposalResult(diff=...)
        agent = MockCodingAgent(callable_=fix)
    """

    def __init__(
        self,
        *,
        static_result: ProposalResult | None = None,
        sequence: list[ProposalResult] | None = None,
        callable_: _AgentFn | None = None,
        review_result: ReviewResult | None = None,
        review_sequence: list[ReviewResult] | None = None,
        review_callable: _ReviewFn | None = None,
    ) -> None:
        provided = sum(x is not None for x in (static_result, sequence, callable_))
        if provided != 1:
            raise ValueError(
                "MockCodingAgent requires exactly one of: static_result, sequence, callable_"
            )
        self._static = static_result
        self._sequence = list(sequence) if sequence else None
        self._callable = callable_
        # Judge-Mock: optional. Default (nichts gesetzt) ist ein pass mit
        # score 1.0 — so läuft ein dry-run mit aktiviertem Judge sauber
        # durch, ohne dass Tests jeden Judge-Aufruf konfigurieren müssen.
        self._review_static = review_result
        self._review_sequence = list(review_sequence) if review_sequence else None
        self._review_callable = review_callable
        self.calls: list[tuple[Path, str]] = []
        self.resume_session_ids: list[str | None] = []
        self.review_calls: list[tuple[Path, str, str]] = []

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
        self.calls.append((worktree, prompt))
        self.resume_session_ids.append(resume_session_id)

        if self._static is not None:
            return self._static
        if self._sequence is not None:
            if not self._sequence:
                raise IndexError("MockCodingAgent sequence exhausted")
            return self._sequence.pop(0)
        assert self._callable is not None

        # Capture base SHA to compute diff after the callable
        base_sha = _git_rev_parse(worktree, "HEAD")
        out = self._callable(worktree, prompt)
        if isinstance(out, ProposalResult):
            return out

        # Wenn der Callable nichts zurückgibt, bauen wir das Result aus dem
        # Worktree-Stand selbst. Das ist der bequeme Modus für Tests, die
        # nur das Mutationsverhalten betrachten.
        diff = _git_diff(worktree, base_sha)
        return ProposalResult(
            diff=diff,
            tokens_in=0,
            tokens_out=0,
            cost_usd=Decimal("0"),
            stop_reason="end_turn",
            turns_used=1,
        )

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
        self.review_calls.append((worktree, acceptance_criteria, diff))
        if self._review_callable is not None:
            return self._review_callable(worktree, acceptance_criteria, diff)
        if self._review_sequence is not None:
            if not self._review_sequence:
                raise IndexError("MockCodingAgent review_sequence exhausted")
            return self._review_sequence.pop(0)
        if self._review_static is not None:
            return self._review_static
        # Default: pass mit voller Punktzahl (dry-run-freundlich).
        return ReviewResult(judge_score=1.0, verdict="pass", reasoning="mock judge")


# --- Local git helpers (Mock soll forge-core nicht querbenötigen) ---


def _git_rev_parse(worktree: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def _git_diff(worktree: Path, base_sha: str) -> str:
    # diff zwischen base und HEAD plus uncommittete Änderungen
    head_diff = subprocess.run(
        ["git", "diff", base_sha, "HEAD"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    wt_diff = subprocess.run(
        ["git", "diff"],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
        errors="replace",
    ).stdout
    if wt_diff.strip():
        if head_diff and not head_diff.endswith("\n"):
            head_diff += "\n"
        return head_diff + wt_diff
    return head_diff


# Type-Check: erfüllt das Protocol
_: type[CodingAgent] = MockCodingAgent
