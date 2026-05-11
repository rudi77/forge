"""`forge board-loop` — pull ready bug-issues from a GitHub Project board
and dispatch each as a normal ``forge run --trigger issue_label`` (Spec v0.4).

Architektur:
* Reine Orchestrations-Schicht über :func:`forge_cli.run.execute_run`.
* Optionale Pre-Phase ``IssueTriage`` (Spec v0.4 Teil 6.3), die per
  ``triage.enabled`` aktiviert wird — emittiert genau ein
  ``IssueTriaged``-Event pro Issue.
* Idempotenz + Filter im Adapter (``forge_adapters.github.board``).
* ``--auto-merge`` durchgereicht an jeden dispatched Run; Spec-Vertrag
  bleibt intakt (forge ruft selbst kein ``gh pr merge`` synchron auf,
  nur ``--auto`` als server-seitiges Queue).
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from forge_adapters.github import (
    BoardError,
    ReadyIssue,
    list_ready_items,
    wrap_issue_body,
)
from forge_core.events import (
    EventKind,
    IssueTriagedPayload,
    build_event,
)
from forge_execute.capabilities import Capabilities
from forge_execute.triage import (
    IssueTriager,
    LLMTriager,
    TriageError,
    TriageResult,
    close_issue,
    comment_issue,
)
from forge_execute.triage.gh import GHTriageError
from forge_execute.worktrees import GitError, WorktreeManager
from rich.table import Table
from ulid import ULID

from forge_cli.run import RunOutcome, execute_run
from forge_cli.runtime import (
    ContextError,
    ForgeContext,
    console,
    err_console,
    load_context,
)

# Wir teilen denselben SubprocessRunner-DI-Pattern wie pr.py / board.py,
# damit Tests gh-Aufrufe stubben können.
SubprocessRunner = Callable[..., subprocess.CompletedProcess]


def board_loop_command(
    spec_path: Annotated[
        Path | None,
        typer.Option(
            "--spec",
            help="Pfad zur project.yaml. Default: <repo>/.forge/project.yaml",
        ),
    ] = None,
    max_issues: Annotated[
        int,
        typer.Option(
            "--max",
            "-n",
            help="Max. Anzahl Issues pro board-loop Aufruf.",
            min=1,
        ),
    ] = 3,
    max_iterations: Annotated[
        int,
        typer.Option(
            "--max-iterations",
            help="Max. Generations pro dispatched Run.",
        ),
    ] = 3,
    max_turns: Annotated[
        int,
        typer.Option(
            "--max-turns",
            help="Max. Tool-Turns pro Generation des dispatched Runs.",
        ),
    ] = 8,
    eval_suite: Annotated[
        str,
        typer.Option("--eval-suite", help="Eval-Suite-Name aus der Spec."),
    ] = "quick",
    base_ref: Annotated[
        str,
        typer.Option("--base", help="Git-Ref als Worktree-Basis pro Run."),
    ] = "HEAD",
    model: Annotated[
        str | None,
        typer.Option("--model", help="Claude-Modell (sonnet/opus). Default: aus Spec."),
    ] = None,
    multi_agent: Annotated[
        bool,
        typer.Option(
            "--multi-agent",
            help="Multi-Agent (architect/developer/tester) pro dispatched Run.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Liste die ready-Items, ohne forge run zu starten.",
        ),
    ] = False,
    auto_merge: Annotated[
        bool,
        typer.Option(
            "--auto-merge",
            help=(
                "Nach jedem PR-Open ``gh pr merge --auto`` aufrufen, sodass "
                "GitHub server-seitig mergt sobald CI grün ist. Repo muss "
                "Auto-Merge in Settings aktiviert haben."
            ),
        ),
    ] = False,
    pr_base: Annotated[
        str,
        typer.Option("--pr-base", help="Ziel-Branch für jeden PR."),
    ] = "main",
    pr_label: Annotated[
        list[str] | None,
        typer.Option("--pr-label", help="Zusätzliches Label pro PR."),
    ] = None,
    claude_bin: Annotated[
        str,
        typer.Option("--claude-bin", help="Claude-CLI-Binary."),
    ] = "claude",
    issue_overrides: Annotated[
        list[int] | None,
        typer.Option(
            "--issue",
            help=(
                "Statt Board-Lookup: nur diese Issues abarbeiten. "
                "Mehrfach verwendbar. Überschreibt --max."
            ),
        ),
    ] = None,
    no_gc: Annotated[
        bool,
        typer.Option(
            "--no-gc",
            help=(
                "Skipt das Garbage-Collection (verwaiste forge/* Worktrees "
                "und lokale Branches ohne Remote). Default: GC läuft."
            ),
        ),
    ] = False,
) -> None:
    """Pull ready issues from the configured GitHub Project, dispatch each
    via the standard issue_label trigger pipeline."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    repo_owner, repo_name = _detect_repo_slug(ctx.repo_root)

    # ---- Garbage-Collection vor Loop-Start --------------------------
    # Verwaiste forge/* Worktrees (frühere Crashes) und lokale Branches,
    # deren Remote-Tracking [gone] ist (Auto-Merge mit --delete-branch),
    # werden hier aufgeräumt. Operator kann das mit --no-gc abschalten.
    if not no_gc:
        _run_garbage_collection(ctx.repo_root)

    # ---- Issue-Liste bestimmen --------------------------------------
    if issue_overrides:
        try:
            ready = _fetch_issues_by_number(
                repo_owner=repo_owner,
                repo_name=repo_name,
                numbers=issue_overrides,
            )
        except BoardError as exc:
            err_console.print(f"[red]error[/red]: {exc}")
            raise typer.Exit(code=2) from None
    else:
        if ctx.spec.board is None:
            err_console.print(
                "[red]error[/red]: spec has no `board:` block. Add one to "
                ".forge/project.yaml or pass --issue NUMBER explicitly."
            )
            raise typer.Exit(code=2)
        try:
            ready = list_ready_items(
                ctx.spec.board,
                repo_owner=repo_owner,
                repo_name=repo_name,
            )
        except BoardError as exc:
            err_console.print(f"[red]error[/red]: {exc}")
            raise typer.Exit(code=2) from None
        ready = ready[:max_issues]

    if not ready:
        console.print("[yellow]Backlog leer[/yellow] — no ready issues.")
        raise typer.Exit(code=0)

    # ---- Dry-run: Tabelle, kein dispatch ----------------------------
    if dry_run:
        _print_dry_run_table(ready, repo_owner, repo_name)
        raise typer.Exit(code=0)

    # ---- Triage-Setup (optional) ------------------------------------
    triage_cfg = ctx.spec.triage
    triager: IssueTriager | None = (
        _build_triager(claude_bin=claude_bin, model=model, ctx=ctx)
        if triage_cfg.enabled
        else None
    )
    capabilities = Capabilities(ctx.spec) if triage_cfg.enabled else None

    # ---- Dispatch ----------------------------------------------------
    summaries: list[_LoopSummaryRow] = []
    bailed = False

    focus_template = (
        ctx.spec.board.default_focus_template if ctx.spec.board else "issue-{number}"
    )
    template_id = (
        ctx.spec.board.default_template_id if ctx.spec.board else "board_loop_v1"
    )

    for issue in ready:
        if triager is not None and capabilities is not None:
            triage_outcome = _run_triage(
                ctx=ctx,
                issue=issue,
                triager=triager,
                capabilities=capabilities,
            )
            if not triage_outcome.dispatch:
                summaries.append(triage_outcome.summary_row)
                continue

        focus = focus_template.format(number=issue.number)
        prompt = wrap_issue_body(title=issue.title, body=issue.body)
        console.print(
            f"\n[bold cyan]>>> board-loop[/bold cyan] dispatching issue "
            f"#{issue.number} [italic]{issue.title}[/italic]"
        )
        try:
            outcome = execute_run(
                ctx=ctx,
                rendered_prompt=prompt,
                prompt_template_id=template_id,
                trigger="issue_label",
                focus=focus,
                base_ref=base_ref,
                max_iterations=max_iterations,
                max_turns=max_turns,
                eval_suite=eval_suite,
                model=model,
                issue_number=issue.number,
                pr_number=None,
                dry_run=False,
                claude_bin=claude_bin,
                multi_agent=multi_agent,
                create_pr=True,
                pr_base=pr_base,
                extra_labels=[*(pr_label or []), f"issue-{issue.number}"],
                pr_draft=False,
                auto_merge=auto_merge,
                announce=False,
            )
        except Exception as exc:
            summaries.append(
                _LoopSummaryRow(
                    issue_number=issue.number,
                    issue_title=issue.title,
                    decision="error",
                    pr_url=None,
                    auto_merge="-",
                    error=str(exc),
                )
            )
            err_console.print(
                f"[red]error[/red] processing issue #{issue.number}: {exc}"
            )
            bailed = True
            break

        summaries.append(_summary_row_from_outcome(issue, outcome))

        # Hard-stop bei Cost-Cap oder anderen Run-Aborts — sonst stapeln
        # sich kaputte Runs unsichtbar.
        if outcome.result.decision in {"cost_cap_hit", "guardrail_blocked", "error"}:
            err_console.print(
                f"[yellow]board-loop bailing[/yellow]: last run decision = "
                f"{outcome.result.decision}"
            )
            bailed = True
            break

    _print_loop_summary(summaries, bailed=bailed)
    raise typer.Exit(code=0 if not bailed else 1)


# --- Helpers -----------------------------------------------------------


@dataclass
class _TriageOutcome:
    """Was eine Triage-Iteration dem Loop-Body mitteilt."""

    dispatch: bool
    """``True`` = normaler ``execute_run`` für dieses Issue. ``False`` =
    überspringen, Summary-Row direkt einfügen."""

    summary_row: _LoopSummaryRow
    """Wird nur ausgewertet, wenn ``dispatch`` False ist."""


def _build_triager(
    *, claude_bin: str, model: str | None, ctx: ForgeContext
) -> IssueTriager:
    """Factory für den produktiven Triager.

    Tests monkeypatchen diese Funktion, um einen ``FakeTriager``
    einzuschleusen, ohne dass dafür ein typer-Flag exposed werden muss.
    """
    triage_cfg = ctx.spec.triage
    return LLMTriager(
        claude_bin=claude_bin,
        model=triage_cfg.model or model,
        max_turns=triage_cfg.max_turns,
    )


def _run_triage(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    triager: IssueTriager,
    capabilities: Capabilities,
) -> _TriageOutcome:
    """Triagiert ein Issue, emittiert das Event und führt optionale
    Side-Effects (Kommentar/Close) aus.

    Bei TriageError oder Triager-Crash wird mit ``relevant`` weitergemacht
    — der Hauptpfad darf nicht hängen, weil das Vorab-Klassifikat
    daneben lag.
    """
    triage_cfg = ctx.spec.triage
    console.print(
        f"[dim]triage:[/dim] classifying issue "
        f"#{issue.number} [italic]{issue.title}[/italic]"
    )
    try:
        result = triager.triage(issue=issue, repo_root=ctx.repo_root)
    except TriageError as exc:
        err_console.print(
            f"[yellow]triage failed for #{issue.number}[/yellow]: {exc}"
        )
        result = TriageResult(
            decision="relevant", reason=f"triage error fallback: {exc}"
        )

    run_id = str(ULID())
    _emit_triage_event(ctx=ctx, issue=issue, result=result, run_id=run_id)

    if result.is_relevant:
        console.print(
            f"[dim]triage:[/dim] #{issue.number} → relevant "
            f"({_short(result.reason)})"
        )
        return _TriageOutcome(
            dispatch=True,
            summary_row=_LoopSummaryRow(
                issue_number=issue.number,
                issue_title=issue.title,
                decision="triaged_relevant",
                pr_url=None,
                auto_merge="-",
            ),
        )

    # Nicht relevant: side-effects + skip
    console.print(
        f"[yellow]triage:[/yellow] #{issue.number} → {result.decision} "
        f"({_short(result.reason)})"
    )
    if triage_cfg.auto_comment:
        if capabilities.check_action("comment_issue").allowed:
            try:
                comment_issue(
                    repo=ctx.repo_root,
                    issue_number=issue.number,
                    body=_format_triage_comment(result),
                )
            except GHTriageError as exc:
                err_console.print(
                    f"[yellow]triage comment failed for #{issue.number}[/yellow]: {exc}"
                )
        else:
            err_console.print(
                f"[yellow]triage[/yellow]: comment_issue capability denied, "
                f"skipping comment on #{issue.number}"
            )

    if triage_cfg.auto_close:
        if capabilities.check_action("close_issue").allowed:
            close_reason = (
                "completed" if result.decision == "already_solved" else "not planned"
            )
            try:
                close_issue(
                    repo=ctx.repo_root,
                    issue_number=issue.number,
                    reason=close_reason,
                )
            except GHTriageError as exc:
                err_console.print(
                    f"[yellow]triage close failed for #{issue.number}[/yellow]: {exc}"
                )
        else:
            err_console.print(
                f"[yellow]triage[/yellow]: close_issue capability denied, "
                f"keeping #{issue.number} open"
            )

    return _TriageOutcome(
        dispatch=False,
        summary_row=_LoopSummaryRow(
            issue_number=issue.number,
            issue_title=issue.title,
            decision=f"triaged_{result.decision}",
            pr_url=None,
            auto_merge="-",
        ),
    )


def _emit_triage_event(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    result: TriageResult,
    run_id: str,
) -> None:
    """Schreibt genau ein ``IssueTriaged``-Event in den Store.

    Eigene ``run_id`` pro Triage — Triage und nachgelagerter Dispatch
    sind separate logische Runs (auch wenn der Dispatch ausbleibt).
    Korrelations-Anker zwischen beiden ist ``issue_number`` im Payload.
    """
    payload = IssueTriagedPayload(
        issue_number=issue.number,
        decision=result.decision,
        reason=result.reason,
        related_pr=result.related_pr,
        related_commit=result.related_commit,
        turns_used=result.turns_used,
    )
    evt = build_event(
        kind=EventKind.ISSUE_TRIAGED,
        run_id=run_id,
        project=ctx.spec.name,
        project_fingerprint=ctx.project_fingerprint,
        factory_version=ctx.factory_version,
        spec_version=ctx.spec.spec_version,
        payload=payload,
        cost_usd=result.cost_usd,
        model=result.model,
    )
    store = ctx.open_store()
    try:
        store.append(evt)
    finally:
        store.close()


def _format_triage_comment(result: TriageResult) -> str:
    """Baut den Issue-Kommentar zusammen, der beim Auto-Close gespiegelt wird."""
    label = {
        "stale": "veraltet",
        "duplicate": "Duplikat",
        "already_solved": "bereits gelöst",
        "relevant": "relevant",  # praktisch nie aufgerufen
    }[result.decision]
    parts = [
        f"_forge triage: **{label}**_",
        "",
        result.reason or "_(keine Begründung)_",
    ]
    if result.related_pr is not None:
        parts.append(f"\nVerwandter PR/Issue: #{result.related_pr}")
    if result.related_commit:
        parts.append(f"\nVerwandter Commit: `{result.related_commit}`")
    parts.append("")
    parts.append(
        "Falls die Einschätzung daneben liegt, einfach erneut öffnen — "
        "forge triagiert beim nächsten board-loop wieder."
    )
    return "\n".join(parts)


def _short(text: str, limit: int = 80) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _run_garbage_collection(repo_root: Path) -> None:
    """Räumt verwaiste Worktrees + lokale forge/* Branches auf.

    Best-effort: bei Git-Problemen warnt die Funktion auf stderr, lässt
    den board-loop aber weiterlaufen. Eine kaputte GC darf nicht den
    Hauptpfad blockieren.
    """
    wm = WorktreeManager(repo_root)
    try:
        removed_worktrees = wm.gc_stale()
    except GitError as exc:
        err_console.print(f"[yellow]gc warning[/yellow]: worktree cleanup failed: {exc}")
        removed_worktrees = []
    try:
        removed_branches = wm.prune_merged_branches()
    except GitError as exc:
        err_console.print(f"[yellow]gc warning[/yellow]: branch cleanup failed: {exc}")
        removed_branches = []
    if removed_worktrees or removed_branches:
        console.print(
            f"[dim]gc:[/dim] pruned {len(removed_worktrees)} stale worktree(s), "
            f"{len(removed_branches)} merged branch(es)"
        )


_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?/?$"
)


def _detect_repo_slug(
    repo: Path,
    *,
    run_subprocess: SubprocessRunner = subprocess.run,
) -> tuple[str, str]:
    """``git remote get-url origin`` → (owner, repo). Funktioniert mit ssh
    und https Remotes."""
    result = run_subprocess(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise typer.BadParameter(
            f"git remote get-url origin failed: {result.stderr.strip()}"
        )
    url = result.stdout.strip()
    match = _REMOTE_RE.search(url)
    if not match:
        raise typer.BadParameter(
            f"could not parse owner/repo from remote URL {url!r}"
        )
    return match.group("owner"), match.group("repo")


def _fetch_issues_by_number(
    *,
    repo_owner: str,
    repo_name: str,
    numbers: list[int],
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> list[ReadyIssue]:
    """Lädt Issue-Daten via ``gh issue view --json`` für die gegebenen Nummern.

    Wird vom ``--issue 42 43``-Override-Pfad gebraucht — wir gehen nicht
    übers Project, also kein Board-Filter, kein Idempotenz-Check.
    Operator weiß was er tut.
    """
    out: list[ReadyIssue] = []
    for n in numbers:
        cmd = [
            gh_bin, "issue", "view", str(n),
            "--repo", f"{repo_owner}/{repo_name}",
            "--json", "number,title,body,labels,state,url",
        ]
        result = run_subprocess(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            raise BoardError(
                f"gh issue view #{n} failed (exit {result.returncode}): "
                f"{result.stderr.strip()}"
            )
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BoardError(
                f"gh issue view #{n} returned invalid JSON: {exc}"
            ) from exc
        labels_raw = data.get("labels") or []
        labels = [
            entry.get("name", "") for entry in labels_raw if isinstance(entry, dict)
        ]
        out.append(
            ReadyIssue(
                number=int(data.get("number", n)),
                title=str(data.get("title", "")),
                body=str(data.get("body", "")),
                labels=[lbl for lbl in labels if lbl],
                project_status="(override)",
                url=str(data.get("url", "")),
            )
        )
    return out


# --- Output helpers ----------------------------------------------------


@dataclass
class _LoopSummaryRow:
    issue_number: int
    issue_title: str
    decision: str
    pr_url: str | None
    auto_merge: str
    error: str | None = None


def _summary_row_from_outcome(
    issue: ReadyIssue, outcome: RunOutcome
) -> _LoopSummaryRow:
    if outcome.auto_merge_queued:
        am = "[green]queued[/green]"
    elif outcome.auto_merge_error:
        am = f"[red]{outcome.auto_merge_error[:40]}[/red]"
    else:
        am = "-"
    return _LoopSummaryRow(
        issue_number=issue.number,
        issue_title=issue.title,
        decision=outcome.result.decision,
        pr_url=outcome.pr_url,
        auto_merge=am,
        error=outcome.pr_error,
    )


def _print_dry_run_table(
    ready: list[ReadyIssue], repo_owner: str, repo_name: str
) -> None:
    table = Table(
        title=f"board-loop dry-run · {repo_owner}/{repo_name} · {len(ready)} ready",
        show_lines=False,
    )
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("status", style="magenta")
    table.add_column("labels")
    table.add_column("title", overflow="fold")
    for r in ready:
        table.add_row(
            str(r.number),
            r.project_status,
            ", ".join(r.labels) or "-",
            r.title,
        )
    console.print(table)


def _print_loop_summary(
    summaries: list[_LoopSummaryRow], *, bailed: bool
) -> None:
    title = "board-loop summary" + (" (BAILED)" if bailed else "")
    style = "red" if bailed else "green"
    table = Table(title=title, border_style=style)
    table.add_column("#", style="cyan", no_wrap=True)
    table.add_column("decision")
    table.add_column("PR")
    table.add_column("auto-merge")
    table.add_column("title", overflow="fold")
    for s in summaries:
        table.add_row(
            str(s.issue_number),
            s.decision,
            s.pr_url or (s.error or "-"),
            s.auto_merge,
            s.issue_title,
        )
    console.print(table)
