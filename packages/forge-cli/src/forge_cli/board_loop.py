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

import contextlib
import json
import re
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from forge_adapters.github import (
    BoardError,
    ReadyIssue,
    list_ready_items,
    list_stage_items,
    set_issue_stage_label,
    wrap_issue_body,
)
from forge_core.events import (
    ConductorTickCompletedPayload,
    EventKind,
    IssueTriagedPayload,
    WorkItemBlockedPayload,
    WorkItemStageChangedPayload,
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

from forge_cli.conductor import (
    Blocked,
    DispatchOrder,
    ResumeOrder,
    StageTransition,
    WorkItem,
    derive_pending_resumes,
    derive_signals,
    run_conductor_tick,
)
from forge_cli.dependencies import parse_depends_on
from forge_cli.heartbeat import HeartbeatStats, TickResult, run_heartbeat
from forge_cli.run import _DEFAULT_RESUME_PROMPT, RunOutcome, execute_run
from forge_cli.runtime import (
    ContextError,
    ForgeContext,
    console,
    err_console,
    load_context,
)
from forge_cli.stages import Stage, stage_of

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
    watch: Annotated[
        bool,
        typer.Option(
            "--watch",
            help=(
                "Dauerbetrieb (Conductor Phase B): das Board kontinuierlich "
                "pollen und abarbeiten, statt eines einmaligen Durchlaufs. "
                "Mit Ctrl-C sauber beenden (laufender Run wird zu Ende "
                "gebracht). Nicht mit --issue kombinierbar."
            ),
        ),
    ] = False,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Sekunden Pause zwischen zwei Heartbeat-Ticks (nur --watch).",
            min=1.0,
        ),
    ] = 300.0,
    conductor: Annotated[
        bool,
        typer.Option(
            "--conductor",
            help=(
                "Conductor-Modus (nur --watch): statt nur board-ready Bugs "
                "abzuarbeiten, fährt forge die Stage-State-Machine über alle "
                "`forge:`-Stage-Labels — Übergänge (design→ready→in-dev→qa→"
                "release), Dependency-Reihenfolge (`Depends-On: #N` im Body) "
                "und Dispatch mit Kapazität 1."
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

    # ---- Triage-Setup + Dispatch-Parameter --------------------------
    triage_cfg = ctx.spec.triage
    triager: IssueTriager | None = (
        _build_triager(claude_bin=claude_bin, model=model, ctx=ctx)
        if triage_cfg.enabled
        else None
    )
    capabilities = Capabilities(ctx.spec) if triage_cfg.enabled else None

    focus_template = (
        ctx.spec.board.default_focus_template if ctx.spec.board else "issue-{number}"
    )
    template_id = (
        ctx.spec.board.default_template_id if ctx.spec.board else "board_loop_v1"
    )
    params = _DispatchParams(
        template_id=template_id,
        focus_template=focus_template,
        base_ref=base_ref,
        max_iterations=max_iterations,
        max_turns=max_turns,
        eval_suite=eval_suite,
        model=model,
        claude_bin=claude_bin,
        multi_agent=multi_agent,
        auto_merge=auto_merge,
        pr_base=pr_base,
        pr_label=pr_label,
    )

    # ---- Watch-Modus: Dauerbetrieb (Conductor Phase B) --------------
    if watch:
        if issue_overrides:
            err_console.print(
                "[red]error[/red]: --watch und --issue sind nicht kombinierbar "
                "(--watch pollt das Board kontinuierlich)."
            )
            raise typer.Exit(code=2)
        if ctx.spec.board is None:
            err_console.print(
                "[red]error[/red]: --watch braucht einen `board:`-Block in der "
                "Spec."
            )
            raise typer.Exit(code=2)
        watch_fn = _run_conductor_watch if conductor else _run_watch
        stats = watch_fn(
            ctx=ctx,
            repo_owner=repo_owner,
            repo_name=repo_name,
            max_issues=max_issues,
            interval_s=interval,
            params=params,
            triager=triager,
            capabilities=capabilities,
        )
        console.print(
            f"\n[bold]heartbeat gestoppt[/bold] ({stats.stopped_reason}) — "
            f"{stats.ticks} Ticks, {stats.total_dispatched} Runs dispatcht."
        )
        raise typer.Exit(code=0)

    # ---- Single-Pass: Issue-Liste bestimmen -------------------------
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

    if dry_run:
        _print_dry_run_table(ready, repo_owner, repo_name)
        raise typer.Exit(code=0)

    result = _dispatch_issues(
        ctx=ctx,
        issues=ready,
        params=params,
        triager=triager,
        capabilities=capabilities,
    )
    _print_loop_summary(result.summaries, bailed=result.bailed)
    raise typer.Exit(code=0 if not result.bailed else 1)


# --- Helpers -----------------------------------------------------------


@dataclass
class _DispatchParams:
    """Run-Parameter, die für jedes dispatchte Issue gleich sind.

    Gebündelt, damit ``_dispatch_issues`` nicht ein Dutzend Einzelargumente
    durchreichen muss — und damit der Single-Pass und der Watch-Tick exakt
    denselben Dispatch-Pfad nutzen.
    """

    template_id: str
    focus_template: str
    base_ref: str
    max_iterations: int
    max_turns: int
    eval_suite: str
    model: str | None
    claude_bin: str
    multi_agent: bool
    auto_merge: bool
    pr_base: str
    pr_label: list[str] | None


@dataclass
class _PassResult:
    """Ergebnis eines Board-Pass (eine Iteration über die ready-Issues)."""

    summaries: list[_LoopSummaryRow]
    bailed: bool
    dispatched: int
    skipped: int


def _dispatch_issues(
    *,
    ctx: ForgeContext,
    issues: list[ReadyIssue],
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
) -> _PassResult:
    """Arbeitet eine Liste ready-Issues ab (Triage → execute_run → Summary).

    Bricht ab (``bailed=True``), sobald ein Run mit Cost-Cap/Guardrail/Fehler
    endet — sonst stapeln sich kaputte Runs unsichtbar. Identisches Verhalten
    wie der frühere Inline-Loop; nur extrahiert, damit Single-Pass und
    Watch-Tick denselben Code teilen.
    """
    summaries: list[_LoopSummaryRow] = []
    bailed = False
    dispatched = 0
    skipped = 0

    for issue in issues:
        if triager is not None and capabilities is not None:
            triage_outcome = _run_triage(
                ctx=ctx,
                issue=issue,
                triager=triager,
                capabilities=capabilities,
            )
            if not triage_outcome.dispatch:
                summaries.append(triage_outcome.summary_row)
                skipped += 1
                continue

        focus = params.focus_template.format(number=issue.number)
        # Roster aus der Trigger-Config ableiten (Spec v0.3 Teil 5.1): das
        # erste Issue-Label, das in `triggers.on_issue_label` konfiguriert
        # ist, bestimmt, welche Arbeitspferde mitwirken. Kein Treffer → der
        # multi_agent-Default in execute_run greift.
        roster = _roster_for_issue(ctx.spec, issue.labels)
        prompt = wrap_issue_body(title=issue.title, body=issue.body)
        # Der vom Menschen geschriebene Issue-Text IST das Akzeptanz-
        # kriterium für den LLM-Judge (spec.judge.enabled). Wir geben den
        # rohen Titel+Body durch, nicht den UNTRUSTED-gewrappten Prompt —
        # der Judge bewertet gegen die Anforderung, nicht gegen die
        # Sicherheits-Hülle.
        acceptance = f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}"
        console.print(
            f"\n[bold cyan]>>> board-loop[/bold cyan] dispatching issue "
            f"#{issue.number} [italic]{issue.title}[/italic]"
        )
        try:
            outcome = execute_run(
                ctx=ctx,
                rendered_prompt=prompt,
                prompt_template_id=params.template_id,
                trigger="issue_label",
                focus=focus,
                base_ref=params.base_ref,
                acceptance_criteria=acceptance,
                max_iterations=params.max_iterations,
                max_turns=params.max_turns,
                eval_suite=params.eval_suite,
                model=params.model,
                issue_number=issue.number,
                pr_number=None,
                dry_run=False,
                claude_bin=params.claude_bin,
                multi_agent=params.multi_agent,
                agents=roster,
                create_pr=True,
                pr_base=params.pr_base,
                extra_labels=[*(params.pr_label or []), f"issue-{issue.number}"],
                pr_draft=False,
                auto_merge=params.auto_merge,
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

        dispatched += 1
        summaries.append(_summary_row_from_outcome(issue, outcome))

        if outcome.result.decision in {"cost_cap_hit", "guardrail_blocked", "error"}:
            err_console.print(
                f"[yellow]board-loop bailing[/yellow]: last run decision = "
                f"{outcome.result.decision}"
            )
            bailed = True
            break

    return _PassResult(
        summaries=summaries, bailed=bailed, dispatched=dispatched, skipped=skipped
    )


def _dispatch_design_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    params: _DispatchParams,
) -> _PassResult:
    """Dispatcht den **Design-Stage**-Run eines Work-Items (Team = architect).

    Anders als der Dev-Loop produziert das Design-Team einen **Plan**, keinen
    PR: ``create_pr=False``. Der ``architect``-Subagent emittiert den
    ``---FORGE-PLAN-...---``-Marker → der Runner schreibt ein ``PlanProposed``,
    aus dem ``derive_signals`` im nächsten Tick ``has_plan`` ableitet — und
    ``advance`` das Item ``design→ready`` fortschreibt.

    Roster: ``triggers.on_issue_label["forge:design"].agents``, falls
    konfiguriert (Stage-Label = Trigger-Key); sonst ``["architect"]`` als
    Default. Keine Triage (das Item ist bereits past requirements).
    """
    roster = _roster_for_issue(ctx.spec, issue.labels) or ["architect"]
    prompt = wrap_issue_body(title=issue.title, body=issue.body)
    acceptance = f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}"
    console.print(
        f"\n[bold magenta]>>> board-loop[/bold magenta] design run for issue "
        f"#{issue.number} [italic]{issue.title}[/italic] "
        f"([dim]team: {', '.join(roster)}[/dim])"
    )
    try:
        outcome = execute_run(
            ctx=ctx,
            rendered_prompt=prompt,
            prompt_template_id="design",
            trigger="issue_label",
            focus=f"design:#{issue.number}",
            base_ref=params.base_ref,
            acceptance_criteria=acceptance,
            max_iterations=params.max_iterations,
            max_turns=params.max_turns,
            eval_suite=params.eval_suite,
            model=params.model,
            issue_number=issue.number,
            pr_number=None,
            dry_run=False,
            claude_bin=params.claude_bin,
            multi_agent=False,
            agents=roster,
            create_pr=False,
            pr_base=params.pr_base,
            extra_labels=[],
            pr_draft=False,
            auto_merge=False,
            announce=False,
        )
    except Exception as exc:
        err_console.print(
            f"[red]error[/red] in design run for issue #{issue.number}: {exc}"
        )
        return _PassResult(
            summaries=[
                _LoopSummaryRow(
                    issue_number=issue.number,
                    issue_title=issue.title,
                    decision="error",
                    pr_url=None,
                    auto_merge="-",
                    error=str(exc),
                )
            ],
            bailed=True,
            dispatched=0,
            skipped=0,
        )

    bailed = outcome.result.decision in {
        "cost_cap_hit",
        "guardrail_blocked",
        "error",
    }
    if bailed:
        err_console.print(
            f"[yellow]board-loop bailing[/yellow]: design run decision = "
            f"{outcome.result.decision}"
        )
    return _PassResult(
        summaries=[_summary_row_from_outcome(issue, outcome)],
        bailed=bailed,
        dispatched=1,
        skipped=0,
    )


def _dispatch_resume(
    *,
    ctx: ForgeContext,
    order: ResumeOrder,
    params: _DispatchParams,
    issue: ReadyIssue | None,
) -> _PassResult:
    """Setzt einen vom Usage-/Session-Limit unterbrochenen Run fort (Loop 2).

    Reicht den Resume-Anker (run_id + session_id) an ``execute_run`` durch; der
    Runner dockt an den eingefrorenen Worktree an (``claude --resume``) und führt
    den Task zu Ende. Roster best-effort aus dem Issue-Label (wie der ursprüngliche
    Dispatch); fehlt das Issue im aktuellen Board-Blick, greift der
    ``execute_run``-Default. Mantra 3: das WANN kam aus der reinen
    ``derive_pending_resumes``-Ableitung, nicht aus dem Runner.
    """
    roster = _roster_for_issue(ctx.spec, issue.labels) if issue is not None else None
    title = issue.title if issue is not None else f"resume {order.run_id[:10]}"
    console.print(
        f"\n[bold blue]>>> board-loop[/bold blue] resume run "
        f"[dim]{order.run_id[:10]}[/dim] (issue "
        f"#{order.issue_number if order.issue_number else '?'} — session-limit reset erreicht)"
    )
    try:
        outcome = execute_run(
            ctx=ctx,
            rendered_prompt=_DEFAULT_RESUME_PROMPT,
            prompt_template_id="resume",
            trigger="schedule",
            focus=f"resume:{order.run_id}",
            base_ref=params.base_ref,
            acceptance_criteria=None,
            max_iterations=params.max_iterations,
            max_turns=params.max_turns,
            eval_suite=params.eval_suite,
            model=params.model,
            issue_number=order.issue_number,
            pr_number=None,
            dry_run=False,
            claude_bin=params.claude_bin,
            multi_agent=params.multi_agent,
            agents=roster,
            create_pr=True,
            pr_base=params.pr_base,
            extra_labels=params.pr_label or [],
            pr_draft=False,
            auto_merge=params.auto_merge,
            announce=False,
            resume_run_id=order.run_id,
            resume_session_id=order.resume_session_id,
        )
    except Exception as exc:
        err_console.print(f"[red]error[/red] resuming run {order.run_id}: {exc}")
        return _PassResult(
            summaries=[
                _LoopSummaryRow(
                    issue_number=order.issue_number or 0,
                    issue_title=title,
                    decision="error",
                    pr_url=None,
                    auto_merge="-",
                    error=str(exc),
                )
            ],
            bailed=True,
            dispatched=0,
            skipped=0,
        )

    bailed = outcome.result.decision in {"cost_cap_hit", "guardrail_blocked", "error"}
    return _PassResult(
        summaries=[
            _LoopSummaryRow(
                issue_number=order.issue_number or 0,
                issue_title=title,
                decision=outcome.result.decision,
                pr_url=outcome.pr_url,
                auto_merge="-",
                error=outcome.pr_error,
            )
        ],
        bailed=bailed,
        dispatched=1,
        skipped=0,
    )


def _heartbeat_session(
    *,
    ctx: ForgeContext,
    interval_s: float,
    make_tick: Callable[[Any, str], Callable[[int], TickResult]],
    max_ticks: int | None = None,
) -> HeartbeatStats:
    """Gemeinsame Heartbeat-Mechanik für board-watch UND conductor-watch.

    Vergibt die Session-ULID, öffnet den Store, installiert die Signal-Handler
    (Graceful-Shutdown), emittiert pro Tick ein ``ConductorTickCompleted``
    (Loop 2 steht über den Runs — die Session-ULID ist die ``run_id`` dieser
    Fabrik-Events) und räumt am Ende auf. Der konkrete Tick (Board-Pass vs.
    State-Machine) kommt als ``make_tick(store, session_id)`` herein.
    """
    session_id = str(ULID())
    store = ctx.open_store()

    stop_flag = {"stop": False}

    def _request_stop(_signum: int, _frame: object) -> None:
        stop_flag["stop"] = True
        err_console.print(
            "\n[yellow]stop angefordert[/yellow] — beende nach dem laufenden Tick."
        )

    prev_handlers: list[tuple[int, object]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Nicht im Main-Thread (z.B. Tests) → kein Signal-Handler möglich.
        with contextlib.suppress(ValueError, OSError):
            prev_handlers.append((sig, signal.signal(sig, _request_stop)))

    tick_fn = make_tick(store, session_id)

    def emit(tick_index: int, result: TickResult) -> None:
        store.append(
            build_event(
                kind=EventKind.CONDUCTOR_TICK_COMPLETED,
                run_id=session_id,
                project=ctx.spec.name,
                project_fingerprint=ctx.project_fingerprint,
                factory_version=ctx.factory_version,
                spec_version=ctx.spec.spec_version,
                payload=ConductorTickCompletedPayload(
                    tick_index=tick_index,
                    dispatched=result.dispatched,
                    scheduled=result.scheduled,
                    blocked=result.blocked,
                    skipped=result.skipped,
                    bailed=result.bailed,
                    scheduled_resume_count=result.scheduled_resume_count,
                ),
            )
        )

    console.print(
        f"[bold green]heartbeat[/bold green] gestartet (session {session_id[:10]}, "
        f"interval {interval_s:.0f}s) — Ctrl-C zum Beenden."
    )
    try:
        return run_heartbeat(
            tick_fn=tick_fn,
            interval_s=interval_s,
            sleep=time.sleep,
            should_stop=lambda: stop_flag["stop"],
            emit=emit,
            max_ticks=max_ticks,
            stop_on_bail=False,
        )
    finally:
        for sig, handler in prev_handlers:
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)  # type: ignore[arg-type]
        store.close()


def _run_watch(
    *,
    ctx: ForgeContext,
    repo_owner: str,
    repo_name: str,
    max_issues: int,
    interval_s: float,
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
    max_ticks: int | None = None,
) -> HeartbeatStats:
    """Flacher Dauerbetrieb (Phase B): pollt board-ready Issues und arbeitet
    sie ab — ohne Stage-State-Machine."""

    def make_tick(_store: Any, _session_id: str) -> Callable[[int], TickResult]:
        def tick_fn(tick_index: int) -> TickResult:
            try:
                ready = list_ready_items(
                    ctx.spec.board, repo_owner=repo_owner, repo_name=repo_name
                )[:max_issues]
            except BoardError as exc:
                err_console.print(
                    f"[red]board error[/red] (tick {tick_index}): {exc}"
                )
                return TickResult(bailed=False)
            if not ready:
                console.print(
                    f"[dim]tick {tick_index}: Backlog leer — warte "
                    f"{interval_s:.0f}s[/dim]"
                )
                return TickResult()
            res = _dispatch_issues(
                ctx=ctx,
                issues=ready,
                params=params,
                triager=triager,
                capabilities=capabilities,
            )
            _print_loop_summary(res.summaries, bailed=res.bailed)
            return TickResult(
                dispatched=res.dispatched, skipped=res.skipped, bailed=res.bailed
            )

        return tick_fn

    return _heartbeat_session(
        ctx=ctx, interval_s=interval_s, make_tick=make_tick, max_ticks=max_ticks
    )


def _run_conductor_watch(
    *,
    ctx: ForgeContext,
    repo_owner: str,
    repo_name: str,
    max_issues: int,
    interval_s: float,
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
    max_ticks: int | None = None,
) -> HeartbeatStats:
    """Conductor-Dauerbetrieb (Phase C): fährt die Stage-State-Machine.

    Pro Tick: alle ``forge:``-Stage-Issues laden, ``WorkItem``-Liste bauen
    (Stage aus Labels, Deps aus Body, Signale aus dem Event-Strom), Tick planen
    und effektieren — Label-Übergänge via gh, Dispatch über den bestehenden
    ``execute_run``-Pfad, Kapazität 1. Übergänge und Blockaden werden als
    ``WorkItemStageChanged``/``WorkItemBlocked`` persistiert.
    """
    stage_labels = [s.value for s in Stage]

    def make_tick(store: Any, session_id: str) -> Callable[[int], TickResult]:
        def _emit_stage_changed(t: StageTransition) -> None:
            store.append(
                build_event(
                    kind=EventKind.WORK_ITEM_STAGE_CHANGED,
                    run_id=session_id,
                    project=ctx.spec.name,
                    project_fingerprint=ctx.project_fingerprint,
                    factory_version=ctx.factory_version,
                    spec_version=ctx.spec.spec_version,
                    payload=WorkItemStageChangedPayload(
                        issue_number=t.number,
                        from_stage=t.from_stage.value,
                        to_stage=t.to_stage.value,
                        reason=t.reason,
                    ),
                )
            )

        def _emit_blocked(b: Blocked) -> None:
            store.append(
                build_event(
                    kind=EventKind.WORK_ITEM_BLOCKED,
                    run_id=session_id,
                    project=ctx.spec.name,
                    project_fingerprint=ctx.project_fingerprint,
                    factory_version=ctx.factory_version,
                    spec_version=ctx.spec.spec_version,
                    payload=WorkItemBlockedPayload(
                        issue_number=b.number,
                        kind=b.kind,  # type: ignore[arg-type]
                        blocked_by=list(b.blocked_by),
                        reason=b.reason,
                    ),
                )
            )

        def tick_fn(tick_index: int) -> TickResult:
            try:
                issues = list_stage_items(
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    stage_labels=stage_labels,
                    state="all",
                )
            except BoardError as exc:
                err_console.print(
                    f"[red]board error[/red] (tick {tick_index}): {exc}"
                )
                return TickResult()

            events = []
            for kind in (
                EventKind.RUN_STARTED,
                EventKind.PLAN_PROPOSED,
                EventKind.PR_CREATED,
                EventKind.PR_MERGED,
                EventKind.RUN_RESUME_SCHEDULED,
            ):
                events.extend(store.events_by_kind(kind))

            by_number = {i.number: i for i in issues}

            # --- Fällige Resumes zuerst (Loop 2, Mantra 3) -----------------
            # Vom Usage-Limit unterbrochene Runs, deren reset_at erreicht ist,
            # rein aus dem Event-Strom abgeleitet und über denselben
            # execute_run-Pfad mit --resume fortgesetzt. Dispatch ist synchron →
            # der Resume emittiert ein neues RunStarted (gleiche run_id), das
            # ihn im nächsten Tick als "schon fortgesetzt" markiert (at-most-once).
            resume_count = 0
            resume_bailed = False
            for resume_order in derive_pending_resumes(events, datetime.now(UTC)):
                res = _dispatch_resume(
                    ctx=ctx,
                    order=resume_order,
                    params=params,
                    issue=by_number.get(resume_order.issue_number or -1),
                )
                _print_loop_summary(res.summaries, bailed=res.bailed)
                resume_count += res.dispatched
                resume_bailed = resume_bailed or res.bailed

            items: list[WorkItem] = []
            for issue in issues:
                stage = stage_of(issue.labels)
                # Done bleibt drin (für Dependency-Auflösung), nur BLOCKED raus.
                if stage is None or stage == Stage.BLOCKED:
                    continue
                items.append(
                    WorkItem(
                        number=issue.number,
                        stage=stage,
                        depends_on=tuple(parse_depends_on(issue.body)),
                        signals=derive_signals(events, issue.number),
                    )
                )
            if not items:
                if resume_count == 0:
                    console.print(
                        f"[dim]tick {tick_index}: keine aktiven Work-Items[/dim]"
                    )
                return TickResult(
                    scheduled_resume_count=resume_count, bailed=resume_bailed
                )

            counters = {"dispatched": 0, "bailed": resume_bailed}

            def set_stage(t: StageTransition) -> None:
                set_issue_stage_label(
                    issue_number=t.number,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    add=t.to_stage.value,
                    remove=t.from_stage.value,
                )
                _emit_stage_changed(t)
                console.print(
                    f"  [cyan]#{t.number}[/cyan] {t.from_stage.value} → "
                    f"{t.to_stage.value} ([dim]{t.reason}[/dim])"
                )

            def dispatch(order: DispatchOrder) -> None:
                issue = by_number.get(order.number)
                if issue is None:
                    return
                # Team nach Stage: design → architect-Run (Plan, kein PR),
                # in-dev → der bestehende Dev-Loop (PR). Beide teilen sich die
                # Tick-Kapazität (sequenziell in v1).
                if order.stage == Stage.DESIGN:
                    res = _dispatch_design_run(ctx=ctx, issue=issue, params=params)
                else:
                    res = _dispatch_issues(
                        ctx=ctx,
                        issues=[issue],
                        params=params,
                        triager=triager,
                        capabilities=capabilities,
                    )
                _print_loop_summary(res.summaries, bailed=res.bailed)
                counters["dispatched"] += res.dispatched
                counters["bailed"] = counters["bailed"] or res.bailed

            result = run_conductor_tick(
                items=items,
                capacity=1,
                set_stage=set_stage,
                dispatch=dispatch,
                on_blocked=_emit_blocked,
            )
            return TickResult(
                dispatched=counters["dispatched"],
                blocked=result.blocked,
                bailed=bool(counters["bailed"]),
                scheduled_resume_count=resume_count,
            )

        return tick_fn

    return _heartbeat_session(
        ctx=ctx, interval_s=interval_s, make_tick=make_tick, max_ticks=max_ticks
    )


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


def _roster_for_issue(spec, labels: list[str]) -> list[str] | None:
    """Liefert das Subagent-Roster für ein Issue aus der Trigger-Config.

    Das erste Issue-Label, das als Key in ``triggers.on_issue_label`` steht,
    gewinnt; sein ``agents``-Feld ist das Roster. Kein Treffer (oder keine
    Trigger-Config) → ``None``, dann greift der ``multi_agent``-Default in
    ``execute_run``. So wird die ``agents:[...]``-Spec-Config funktional,
    ohne den Single-Agent-Default zu erzwingen.
    """
    triggers = getattr(spec, "triggers", None)
    if triggers is None:
        return None
    on_label = getattr(triggers, "on_issue_label", None) or {}
    for label in labels:
        cfg = on_label.get(label)
        if cfg is not None:
            return list(cfg.agents)
    return None


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
