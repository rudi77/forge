"""``forge review-pr <N>`` — Agent reviewed einen offenen PR, forge merged opt-in.

Ablauf (alles read-only bis auf das opt-in Review-Posten + Mergen):

1. PR-Kontext sammeln: ``gh pr view`` (Titel, Body, CI-Status, mergeable)
   + ``gh pr diff`` (forge-adapters).
2. Agent urteilen lassen: :class:`forge_execute.pr_review.PRReviewer`
   (fail-closed; verdict ``approve``/``request_changes`` + ``score``).
3. GitHub-Review posten (``gh pr review --approve|--request-changes``) —
   nicht-fatal, wenn GitHub das ablehnt (z.B. eigener PR).
4. Merge-Entscheidung (rein): :func:`forge_execute.pr_review.decide_merge`
   aus Verdikt + Score-Schwelle + CI + ``capabilities.merge_pr``.
5. Bei „merge": ``gh pr merge <N>`` (forge-adapters) + ``PRMerged``-Event.
6. Immer: ``PRReviewed``-Event in den Store.

Die Orchestrierung steckt in :func:`execute_pr_review` (injizierbarer Agent +
``run_subprocess``), damit board-loop/Conductor und Tests denselben Pfad ohne
Typer-Layer nutzen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from forge_adapters.github import (
    GitHubError,
    fetch_pr_diff,
    fetch_pr_metadata,
    merge_pr,
    post_pr_review,
)
from forge_core.events import EventKind, PRMergedPayload, PRReviewedPayload, build_event
from forge_execute.agents import ClaudeCodeCLIAgent, MockCodingAgent
from forge_execute.agents.base import CodingAgent
from forge_execute.capabilities import Capabilities
from forge_execute.pr_review import MergeDecision, PRReviewer, decide_merge
from ulid import ULID

from forge_cli.runtime import ContextError, ForgeContext, console, err_console, load_context


@dataclass
class PRReviewRunOutcome:
    """Strukturiertes Ergebnis eines ``forge review-pr``-Laufs."""

    pr_number: int
    verdict: str
    score: float
    ci_status: str
    merged: bool
    merge_decision: MergeDecision
    review_posted: bool = False
    review_post_error: str | None = None
    merge_error: str | None = None
    reasoning: str = ""


def _pr_created_ts(ctx: ForgeContext, store, pr_number: int) -> datetime | None:
    """ts des ``PRCreated``-Events dieses PRs (für ``time_to_merge_s``)."""
    for evt in store.events_by_kind(EventKind.PR_CREATED):
        if int((evt.payload or {}).get("pr_number", -1)) == pr_number:
            return evt.ts
    return None


def execute_pr_review(
    ctx: ForgeContext,
    *,
    pr_number: int,
    agent: CodingAgent,
    run_subprocess=None,
    merge: bool = True,
    post_review: bool = True,
    threshold: float | None = None,
    max_turns: int = 8,
    budget_usd: Decimal = Decimal("0.50"),
    method: str = "squash",
    delete_branch: bool = True,
    model: str | None = None,
    gh_bin: str = "gh",
    issue_body: str | None = None,
    dry_run: bool = False,
) -> PRReviewRunOutcome:
    """Reviewed (und merged opt-in) einen offenen PR. Effektiert via Events.

    ``run_subprocess`` wird an die gh-Adapter durchgereicht (Tests injizieren
    einen Fake); ``None`` = produktiv ``subprocess.run``.
    """
    import subprocess

    sub = run_subprocess or subprocess.run
    eff_threshold = threshold if threshold is not None else ctx.spec.judge.threshold

    meta = fetch_pr_metadata(
        repo=ctx.repo_root, pr_number=pr_number, gh_bin=gh_bin, run_subprocess=sub
    )
    if meta.state != "OPEN":
        raise ContextError(f"PR #{pr_number} is not open (state={meta.state})")
    diff = fetch_pr_diff(
        repo=ctx.repo_root, pr_number=pr_number, gh_bin=gh_bin, run_subprocess=sub
    )

    reviewer = PRReviewer(agent)
    outcome = reviewer.review_open_pr(
        repo_root=ctx.repo_root,
        pr_title=meta.title,
        pr_body=meta.body,
        pr_diff=diff,
        ci_status=meta.ci_status,
        issue_body=issue_body,
        max_turns=max_turns,
        budget_usd=budget_usd,
        model=model,
    )

    capability_enabled = Capabilities(ctx.spec).check_action("merge_pr").allowed
    decision = decide_merge(
        verdict=outcome.verdict,
        score=outcome.score,
        ci_status=meta.ci_status,
        threshold=eff_threshold,
        capability_enabled=capability_enabled and merge,
    )
    # Konflikt-Guard: GitHub meldet CONFLICTING → niemals mergen.
    if decision.merge and meta.mergeable == "CONFLICTING":
        decision = MergeDecision(merge=False, reason="conflicting")

    result = PRReviewRunOutcome(
        pr_number=pr_number,
        verdict=outcome.verdict,
        score=outcome.score,
        ci_status=meta.ci_status,
        merged=False,
        merge_decision=decision,
        reasoning=outcome.reasoning,
    )

    if dry_run:
        return result

    # GitHub-Review posten (nicht-fatal).
    if post_review:
        try:
            post_pr_review(
                repo=ctx.repo_root,
                pr_number=pr_number,
                approve=outcome.approved,
                body=_review_body(outcome.score, outcome.reasoning),
                gh_bin=gh_bin,
                run_subprocess=sub,
            )
            result.review_posted = True
        except GitHubError as exc:
            result.review_post_error = str(exc)

    store = ctx.open_store()
    try:
        if decision.merge:
            try:
                mres = merge_pr(
                    repo=ctx.repo_root,
                    pr_number=pr_number,
                    method=method,  # type: ignore[arg-type]
                    delete_branch=delete_branch,
                    gh_bin=gh_bin,
                    run_subprocess=sub,
                )
                result.merged = True
                created_ts = _pr_created_ts(ctx, store, pr_number)
                ttm = (
                    int((datetime.now(UTC) - created_ts).total_seconds())
                    if created_ts is not None
                    else 0
                )
                store.append(
                    build_event(
                        kind=EventKind.PR_MERGED,
                        run_id=str(ULID()),
                        project=ctx.spec.name,
                        project_fingerprint=ctx.project_fingerprint,
                        factory_version=ctx.factory_version,
                        spec_version=ctx.spec.spec_version,
                        payload=PRMergedPayload(
                            pr_number=pr_number,
                            merger=mres.merger,
                            time_to_merge_s=ttm,
                            merged_by_forge=True,
                            merge_method=mres.method,
                        ),
                    )
                )
            except GitHubError as exc:
                result.merge_error = str(exc)

        store.append(
            build_event(
                kind=EventKind.PR_REVIEWED,
                run_id=str(ULID()),
                project=ctx.spec.name,
                project_fingerprint=ctx.project_fingerprint,
                factory_version=ctx.factory_version,
                spec_version=ctx.spec.spec_version,
                cost_usd=outcome.cost_usd,
                tokens_in=outcome.tokens_in,
                tokens_out=outcome.tokens_out,
                model=outcome.model,
                duration_ms=outcome.duration_ms,
                payload=PRReviewedPayload(
                    pr_number=pr_number,
                    verdict=outcome.verdict,  # type: ignore[arg-type]
                    score=outcome.score,
                    ci_status=meta.ci_status,  # type: ignore[arg-type]
                    merged=result.merged,
                    review_posted=result.review_posted,
                    merge_blocked_reason=(
                        None if result.merged else (result.merge_error or decision.reason)
                    ),
                ),
            )
        )
    finally:
        store.close()

    return result


def _review_body(score: float, reasoning: str) -> str:
    return (
        f"**forge PR-Review** — merge confidence `{score:.2f}`\n\n"
        f"{reasoning.strip() or '(no reasoning)'}\n\n"
        "> Posted by forge agent review. forge merges only on approve + green CI "
        "with the `merge_pr` capability enabled."
    )


def review_pr_command(
    pr_number: Annotated[int, typer.Argument(help="Nummer des offenen PR.")],
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml. Default: <repo>/.forge/project.yaml"),
    ] = None,
    merge: Annotated[
        bool,
        typer.Option(
            "--merge/--no-merge",
            help="Bei approve + grünem CI mergen (zusätzlich durch capabilities.merge_pr gated).",
        ),
    ] = True,
    post_review: Annotated[
        bool,
        typer.Option("--post-review/--no-post-review", help="GitHub-Review posten."),
    ] = True,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Claude-Modell für den Review-Agent."),
    ] = None,
    threshold: Annotated[
        float | None,
        typer.Option("--score-threshold", help="Merge-Schwelle. Default: spec.judge.threshold."),
    ] = None,
    max_turns: Annotated[int, typer.Option("--max-turns", help="Tool-Turn-Cap.")] = 8,
    budget: Annotated[float, typer.Option("--budget", help="USD-Budget des Review-Calls.")] = 0.50,
    method: Annotated[
        str, typer.Option("--method", help="Merge-Methode: squash|merge|rebase.")
    ] = "squash",
    delete_branch: Annotated[
        bool, typer.Option("--delete-branch/--keep-branch", help="Branch nach Merge löschen.")
    ] = True,
    acceptance_file: Annotated[
        Path | None,
        typer.Option("--acceptance-file", help="Datei mit Akzeptanzkriterien/Issue-Kontext."),
    ] = None,
    gh_bin: Annotated[str, typer.Option("--gh-bin", help="gh-Binary.")] = "gh",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Nur urteilen (MockAgent, kein Review-Post, kein Merge)."),
    ] = False,
) -> None:
    """Reviewt einen offenen PR mit einem Agent und merged ihn opt-in."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    issue_body = acceptance_file.read_text(encoding="utf-8") if acceptance_file else None

    if dry_run:
        console.print("[yellow]dry-run[/yellow]: MockCodingAgent, kein Review-Post/Merge")
        agent: CodingAgent = MockCodingAgent(callable_=lambda wt, prompt: None)
    else:
        agent = ClaudeCodeCLIAgent(default_model=model)

    try:
        outcome = execute_pr_review(
            ctx,
            pr_number=pr_number,
            agent=agent,
            merge=merge,
            post_review=post_review,
            threshold=threshold,
            max_turns=max_turns,
            budget_usd=Decimal(str(budget)),
            method=method,
            delete_branch=delete_branch,
            model=model,
            gh_bin=gh_bin,
            issue_body=issue_body,
            dry_run=dry_run,
        )
    except (ContextError, GitHubError) as exc:
        err_console.print(f"[red]error:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    _render_outcome(outcome)


def _render_outcome(o: PRReviewRunOutcome) -> None:
    color = "green" if o.verdict == "approve" else "yellow"
    console.print(
        f"PR #{o.pr_number}: [{color}]{o.verdict}[/{color}] "
        f"(score {o.score:.2f}, ci {o.ci_status})"
    )
    if o.reasoning:
        console.print(f"  {o.reasoning.strip()[:400]}")
    if o.merged:
        console.print("[green]merged[/green] by forge")
    elif o.merge_error:
        console.print(f"[red]merge failed:[/red] {o.merge_error}")
    elif o.merge_decision.reason:
        console.print(f"[dim]not merged:[/dim] {o.merge_decision.reason}")
    if o.review_post_error:
        console.print(f"[dim]review not posted:[/dim] {o.review_post_error}")
