"""`forge board-loop` — pull ready bug-issues from a GitHub Project board
and dispatch each as a normal ``forge run --trigger issue_label`` (Spec v0.4).

Architektur:
* Reine Orchestrations-Schicht über :func:`forge_cli.run.execute_run`.
* Optionale Pre-Phase ``IssueTriage`` (Spec v0.4 Teil 6.3), die per
  ``triage.enabled`` aktiviert wird — emittiert genau ein
  ``IssueTriaged``-Event pro Issue.
* Idempotenz + Filter im Adapter (``WorkTracker`` aus ``forge_adapters``;
  GitHub, Azure DevOps, …).
* ``--auto-merge`` durchgereicht an jeden dispatched Run; Spec-Vertrag
  bleibt intakt (forge ruft selbst kein ``gh pr merge`` synchron auf,
  nur ``--auto`` als server-seitiges Queue).
"""

from __future__ import annotations

import contextlib
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from forge_adapters.base import CodeHostError, TrackerError, WorkTracker
from forge_adapters.text import wrap_issue_body, wrap_untrusted
from forge_core.events import (
    ConductorTickCompletedPayload,
    EventKind,
    IssueTriagedPayload,
    PRMergedPayload,
    ReleaseTaggedPayload,
    WorkItemBlockedPayload,
    WorkItemStageChangedPayload,
    build_event,
)
from forge_core.store import EventStore
from forge_core.tracking import ReadyIssue
from forge_execute.capabilities import Capabilities
from forge_execute.triage import (
    IssueTriager,
    LLMTriager,
    TriageError,
    TriageResult,
)
from forge_execute.worktrees import GitError, WorktreeManager
from rich.table import Table
from ulid import ULID

from forge_cli.conductor import (
    Blocked,
    DispatchOrder,
    ResumeOrder,
    StageTransition,
    WorkItem,
    derive_dev_failure,
    derive_pending_resumes,
    derive_signals,
    epic_children,
    pr_number_for_issue,
    run_conductor_tick,
    spec_pr_for_issue,
)
from forge_cli.dependencies import (
    parse_depends_on,
    parse_integration_branch,
    parse_integration_mode,
    parse_touches,
)
from forge_cli.heartbeat import HeartbeatStats, TickResult, run_heartbeat
from forge_cli.release import train_tick
from forge_cli.review_pr import execute_pr_review
from forge_cli.run import _DEFAULT_RESUME_PROMPT, RunOutcome, execute_run
from forge_cli.runtime import (
    ContextError,
    ForgeContext,
    console,
    err_console,
    load_context,
)
from forge_cli.stages import Stage, stage_of
from forge_cli.workgen import (
    intake_blocks,
    publish_spec,
    refined_spec_text,
    tick_sources,
    with_refined_spec,
)
from forge_cli.workgraph import (
    ensure_integration_branch,
    epic_run_id,
    integration_branch_name,
    open_integration_pr,
    sync_branch,
    tick_capacity,
)


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
                "und Dispatch mit Kapazität --max-parallel."
            ),
        ),
    ] = False,
    max_parallel: Annotated[
        int,
        typer.Option(
            "--max-parallel",
            help=(
                "Conductor-Kapazität (nur --conductor): bis zu N Runs pro Tick "
                "nebenläufig (eigener Worktree je Run, geteilter Event-Store). "
                "Default 1 = sequenziell wie bisher."
            ),
            min=1,
        ),
    ] = 1,
    max_ticks: Annotated[
        int | None,
        typer.Option(
            "--max-ticks",
            help=(
                "Nur --watch: nach N Ticks sauber beenden (Smoke-/Live-Tests, "
                "Cron-Betrieb). Default: unbegrenzt."
            ),
            min=1,
        ),
    ] = None,
) -> None:
    """Pull ready issues from the configured GitHub Project, dispatch each
    via the standard issue_label trigger pipeline."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    try:
        tracker = ctx.get_tracker()
    except TrackerError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

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
        if max_parallel > 1 and not conductor:
            err_console.print(
                "[red]error[/red]: --max-parallel > 1 braucht --conductor."
            )
            raise typer.Exit(code=2)
        if conductor:
            stats = _run_conductor_watch(
                ctx=ctx,
                tracker=tracker,
                max_issues=max_issues,
                interval_s=interval,
                params=params,
                triager=triager,
                capabilities=capabilities,
                max_parallel=max_parallel,
                max_ticks=max_ticks,
            )
        else:
            stats = _run_watch(
                ctx=ctx,
                tracker=tracker,
                max_issues=max_issues,
                interval_s=interval,
                params=params,
                triager=triager,
                capabilities=capabilities,
                max_ticks=max_ticks,
            )
        console.print(
            f"\n[bold]heartbeat gestoppt[/bold] ({stats.stopped_reason}) — "
            f"{stats.ticks} Ticks, {stats.total_dispatched} Runs dispatcht."
        )
        raise typer.Exit(code=0)

    # ---- Single-Pass: Issue-Liste bestimmen -------------------------
    if issue_overrides:
        try:
            ready = tracker.get_items(list(issue_overrides))
        except TrackerError as exc:
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
            ready = tracker.list_ready_items(ctx.spec.board)
        except TrackerError as exc:
            err_console.print(f"[red]error[/red]: {exc}")
            raise typer.Exit(code=2) from None
        ready = ready[:max_issues]

    if not ready:
        console.print("[yellow]Backlog leer[/yellow] — no ready issues.")
        raise typer.Exit(code=0)

    if dry_run:
        _print_dry_run_table(ready, tracker.provider)
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


@contextlib.contextmanager
def _store_scope(ctx: ForgeContext, store: EventStore | None):
    """Liefert ``store`` oder öffnet/schließt einen eigenen (Single-Pass)."""
    if store is not None:
        yield store
        return
    own = ctx.open_store()
    try:
        yield own
    finally:
        own.close()


def _spec_for(ctx: ForgeContext, store: EventStore | None, issue_number: int) -> str:
    """A1: verdichtete Akzeptanzkriterien des Items (leer, wenn keine)."""
    with _store_scope(ctx, store) as s:
        return refined_spec_text(s, ctx.open_blobs(), issue_number)


def _intake_run_findings(
    ctx: ForgeContext, store: EventStore | None, issue: ReadyIssue, outcome: Any
) -> None:
    """A2: ``FORGE-WORKITEMS``-Funde eines Runs → Items (best-effort)."""
    result = getattr(outcome, "result", None)
    blocks = list(getattr(result, "workitems_blocks", None) or [])
    if not blocks:
        return
    with _store_scope(ctx, store) as s:
        intake_blocks(
            ctx, store=s, run_id=result.run_id, blocks=blocks, source="run_finding",
            origin_issue=issue.number,
        )


def _dispatch_issues(
    *,
    ctx: ForgeContext,
    issues: list[ReadyIssue],
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
    store: EventStore | None = None,
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
                store=store,
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
        spec_md = _spec_for(ctx, store, issue.number)
        prompt = with_refined_spec(wrap_issue_body(title=issue.title, body=issue.body), spec_md)
        # Der vom Menschen geschriebene Issue-Text IST das Akzeptanz-
        # kriterium für den LLM-Judge (spec.judge.enabled). Wir geben den
        # rohen Titel+Body durch, nicht den UNTRUSTED-gewrappten Prompt —
        # der Judge bewertet gegen die Anforderung, nicht gegen die
        # Sicherheits-Hülle. A1: die verdichtete Spec ergänzt ihn.
        acceptance = with_refined_spec(
            f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}", spec_md
        )
        console.print(
            f"\n[bold cyan]>>> board-loop[/bold cyan] dispatching issue "
            f"#{issue.number} [italic]{issue.title}[/italic]"
        )
        # G: Kind eines Integrations-Epics → Basis + PR-Ziel ist forge/epic-<N>.
        base_ref, pr_base = params.base_ref, params.pr_base
        integration = parse_integration_branch(issue.body)
        if integration:
            try:
                base_ref = WorktreeManager(ctx.repo_root).fetch_remote_branch(integration)
                pr_base = integration
            except GitError as exc:
                err_console.print(
                    f"[yellow]integration branch {integration} unavailable[/yellow]: {exc}"
                )
        try:
            outcome = execute_run(
                ctx=ctx,
                rendered_prompt=prompt,
                prompt_template_id=params.template_id,
                trigger="issue_label",
                focus=focus,
                base_ref=base_ref,
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
                pr_base=pr_base,
                extra_labels=[*(params.pr_label or []), f"issue-{issue.number}"],
                pr_draft=False,
                auto_merge=params.auto_merge,
                announce=False,
                store=store,
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
        _intake_run_findings(ctx, store, issue, outcome)

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


def _release_tag_for_issue(issue_number: int) -> str:
    """Deterministischer, eindeutiger Tag pro Work-Item (idempotenter
    Re-Dispatch). v1: ein Release pro abgeschlossenem Issue."""
    return f"forge-issue-{issue_number}"


def _dispatch_release_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    store: EventStore,
    session_id: str,
) -> _PassResult | None:
    """Effektiert die **Release-Stage** eines Work-Items (Pipeline-Ende hinten).

    Anders als die anderen Stages ist das KEIN LLM-Run, sondern ein
    deterministischer forge-Effekt (wie der Merge): Tag + GitHub-Release via
    ``gh release create`` — opt-in über ``capabilities.create_release``.
    Emittiert ``ReleaseTagged`` → ``derive_signals`` leitet ``release_done`` ab
    → ``advance`` schreibt ``release→done`` fort.

    Capability aus → kein Effekt, das Item parkt in release (manueller Operator,
    analog zur ``merge_pr``-Ergonomie). push-to-main/force bleiben unberührt
    (ein Release schreibt nur einen neuen Ref).
    """
    integration = parse_integration_branch(issue.body)
    if integration:
        # G: in den Integrations-Branch gemergt → fertig; ausgeliefert wird mit
        # dem Epic (kein eigener Tag, keine Capability nötig).
        store.append(
            build_event(
                kind=EventKind.RELEASE_TAGGED,
                run_id=session_id,
                project=ctx.spec.name,
                project_fingerprint=ctx.project_fingerprint,
                factory_version=ctx.factory_version,
                spec_version=ctx.spec.spec_version,
                payload=ReleaseTaggedPayload(
                    issue_number=issue.number, tag=integration, integrated_into=integration
                ),
            )
        )
        console.print(f"  [green]integrated[/green] #{issue.number} → {integration}")
        return _PassResult(
            summaries=[_LoopSummaryRow(issue.number, issue.title, "integrated", None, "-")],
            bailed=False, dispatched=1, skipped=0,
        )
    if not ctx.spec.capabilities.create_release:
        console.print(
            f"[dim]release #{issue.number}: capabilities.create_release=false "
            f"→ übersprungen (manueller Release)[/dim]"
        )
        return None
    tag = _release_tag_for_issue(issue.number)
    try:
        url = ctx.get_code_host().create_release(
            tag=tag,
            title=f"{tag}: {issue.title}",
        )
    except CodeHostError as exc:
        err_console.print(
            f"[red]error[/red] in release for issue #{issue.number}: {exc}"
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
            bailed=False,
            dispatched=0,
            skipped=0,
        )
    store.append(
        build_event(
            kind=EventKind.RELEASE_TAGGED,
            run_id=session_id,
            project=ctx.spec.name,
            project_fingerprint=ctx.project_fingerprint,
            factory_version=ctx.factory_version,
            spec_version=ctx.spec.spec_version,
            payload=ReleaseTaggedPayload(
                issue_number=issue.number,
                tag=tag,
                release_url=url or None,
            ),
        )
    )
    console.print(
        f"  [green]released[/green] #{issue.number} → {tag}"
        + (f" ({url})" if url else "")
    )
    return _PassResult(
        summaries=[
            _LoopSummaryRow(
                issue_number=issue.number,
                issue_title=issue.title,
                decision="released",
                pr_url=url or None,
                auto_merge="-",
                error=None,
            )
        ],
        bailed=False,
        dispatched=1,
        skipped=0,
    )


_REQUIREMENTS_PREAMBLE = (
    "This is a REQUIREMENTS-REFINEMENT task, not an implementation task. Do NOT "
    "write or change any code. Read the issue below and produce sharpened, "
    "*testable* acceptance criteria for it inside the plan block — each criterion "
    "concrete enough that a later run can verify it. If the issue is too vague to "
    "refine into testable criteria, mark it as insufficient context instead of "
    "guessing.\n\n"
)


def _dispatch_requirements_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    params: _DispatchParams,
    store: EventStore | None = None,
) -> _PassResult:
    """Dispatcht den **Requirements-Stage**-Run eines Work-Items (Pipeline-Ende
    vorne, Team = architect/analyst).

    Verdichtet ein rohes Issue zu testbaren Akzeptanzkriterien — kein Code, kein
    PR (``create_pr=False``). Reuse der architect/design-Maschinerie: der
    ``---FORGE-PLAN-...---``-Marker trägt hier die Kriterien; der Runner emittiert
    daraus ``RequirementsRefined`` (statt ``PlanProposed``, gated über
    ``prompt_template_id="requirements"``) → ``derive_signals`` leitet
    ``has_refined_spec`` ab → ``advance`` schreibt ``requirements→design`` fort.

    Roster: ``triggers.on_issue_label["forge:requirements"].agents``, sonst
    ``["architect"]`` als Default.
    """
    roster = _roster_for_issue(ctx.spec, issue.labels) or ["architect"]
    prompt = _REQUIREMENTS_PREAMBLE + wrap_issue_body(
        title=issue.title, body=issue.body
    )
    acceptance = f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}"
    console.print(
        f"\n[bold magenta]>>> board-loop[/bold magenta] requirements run for issue "
        f"#{issue.number} [italic]{issue.title}[/italic] "
        f"([dim]team: {', '.join(roster)}[/dim])"
    )
    try:
        outcome = execute_run(
            ctx=ctx,
            rendered_prompt=prompt,
            prompt_template_id="requirements",
            trigger="issue_label",
            focus=f"requirements:#{issue.number}",
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
            store=store,
        )
    except Exception as exc:
        err_console.print(
            f"[red]error[/red] in requirements run for issue #{issue.number}: {exc}"
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
            f"[yellow]board-loop bailing[/yellow]: requirements run decision = "
            f"{outcome.result.decision}"
        )
    # A1: verdichtete Spec als Artefakt veröffentlichen (Spec-PR oder
    # Kommentar). Ohne Veröffentlichung bleibt sie trotzdem Akzeptanzkriterium
    # aller späteren Runs (refined_spec_text).
    if not bailed:
        with _store_scope(ctx, store) as s:
            publish_spec(
                ctx, issue=issue, run_id=outcome.result.run_id, store=s,
                base_ref=params.base_ref, pr_base=params.pr_base,
            )
    return _PassResult(
        summaries=[_summary_row_from_outcome(issue, outcome)],
        bailed=bailed,
        dispatched=1,
        skipped=0,
    )


def _dispatch_design_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    params: _DispatchParams,
    store: EventStore | None = None,
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
    spec_md = _spec_for(ctx, store, issue.number)
    prompt = with_refined_spec(wrap_issue_body(title=issue.title, body=issue.body), spec_md)
    acceptance = with_refined_spec(
        f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}", spec_md
    )
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
            store=store,
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


_REWORK_PREAMBLE = (
    "This is a REWORK task on an EXISTING pull request (PR #{pr}). The working "
    "tree already contains the PR's changes. A reviewer requested changes — "
    "address every blocking finding below with the smallest correct change. Do "
    "not start over and do not touch unrelated code. Re-run the relevant tests "
    "before finishing.\n\n"
)

_CONFLICT_PREAMBLE = (
    "This is a MERGE-CONFLICT task on an EXISTING pull request (PR #{pr}). The "
    "base branch moved after a sibling pull request was merged; forge merged it "
    "into this branch and committed the conflict markers. Resolve every conflict "
    "in the files listed below so that BOTH sides' intent is preserved, remove all "
    "markers, and make the tests pass. Do not drop the sibling's changes.\n\n"
)

_CI_FIX_PREAMBLE = (
    "This is a CI-FIX task on an EXISTING pull request (PR #{pr}). The working "
    "tree already contains the PR's changes, but CI is red. Find the root cause "
    "of the failing checks below and fix it with the smallest correct change. "
    "Never skip, disable or weaken a test to make CI green.\n\n"
)


def _latest_review_reasoning(
    ctx: ForgeContext, events: list, pr_number: int
) -> str:
    """Begründung des jüngsten ``PRReviewed`` für ``pr_number`` (aus dem CAS).

    Best-effort: fehlt der Blob (alte 1.0-Events, Blob-GC), bleibt der Kontext
    leer — der Nacharbeits-Run sieht dann nur das Issue."""
    reviews = [
        e
        for e in events
        if e.kind == EventKind.PR_REVIEWED
        and (e.payload or {}).get("pr_number") == pr_number
    ]
    if not reviews:
        return ""
    blob = (max(reviews, key=lambda e: e.ts).payload or {}).get("reasoning_blob")
    if not blob:
        return ""
    try:
        return ctx.open_blobs().get_text(blob)
    except (FileNotFoundError, OSError, ValueError):
        return ""


def _dispatch_branch_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    pr_number: int,
    params: _DispatchParams,
    kind: str,
    context: str,
    store: EventStore | None = None,
    base_ref_override: str | None = None,
) -> _PassResult:
    """Run auf einem BESTEHENDEN PR-Branch (Nacharbeit L1 / CI-Fix L2 /
    Merge-Konflikt G).

    ``base_ref_override``: Startpunkt statt des PR-Heads (G: der lokale
    Konflikt-Commit aus ``sync_branch``, ein Nachfahre des Heads → der Push
    bleibt fast-forward).

    Der Runner bleibt unverändert: er bekommt als ``base_ref`` den frisch
    geholten PR-Head (``refs/remotes/origin/<head>``) und arbeitet wie immer auf
    einem eigenen ``forge/<run_id>``-Branch. Behält er eine Generation
    (``decision == pr_created``), pusht forge das Ergebnis **fast-forward** auf
    den PR-Head — nie ``--force``, nur auf ``forge/*``-Branches
    (``git_push_argv``). Kein neuer PR: der bestehende wird aktualisiert, sein
    neuer Head-Commit macht das alte Review veraltet → ``in-dev → qa``.
    """
    label = {"rework": "rework", "ci_fix": "ci-fix", "conflict": "conflict"}[kind]
    row = lambda decision, err=None, url=None: _LoopSummaryRow(  # noqa: E731
        issue_number=issue.number,
        issue_title=issue.title,
        decision=decision,
        pr_url=url,
        auto_merge="-",
        error=err,
    )
    code_host = ctx.get_code_host()
    try:
        meta = code_host.fetch_metadata(pr_number)
    except CodeHostError as exc:
        return _PassResult(summaries=[row("error", str(exc))], bailed=False,
                           dispatched=0, skipped=0)
    head = meta.head_branch
    if not head.startswith("forge/"):
        msg = f"PR #{pr_number} head {head!r} is not a forge/* branch — {label} skipped"
        err_console.print(f"[yellow]skip[/yellow] {msg}")
        return _PassResult(summaries=[row("skipped", msg)], bailed=False,
                           dispatched=0, skipped=1)
    try:
        base_ref = base_ref_override or WorktreeManager(ctx.repo_root).fetch_remote_branch(
            head
        )
    except GitError as exc:
        return _PassResult(summaries=[row("error", str(exc))], bailed=False,
                           dispatched=0, skipped=0)

    preamble = {"rework": _REWORK_PREAMBLE, "ci_fix": _CI_FIX_PREAMBLE,
                "conflict": _CONFLICT_PREAMBLE}[kind]
    context_label = {"rework": "review findings", "ci_fix": "ci failure",
                     "conflict": "merge conflict"}[kind]
    prompt = preamble.format(pr=pr_number) + wrap_issue_body(
        title=issue.title, body=issue.body
    )
    if context.strip():
        prompt += "\n" + wrap_untrusted(label=context_label, text=context)
    acceptance = with_refined_spec(
        f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}",
        _spec_for(ctx, store, issue.number),
    )
    if context.strip():
        acceptance += f"\n\n{context_label.title()}:\n{context}"
    roster = (
        _ci_fix_roster(ctx.spec)
        if kind == "ci_fix"
        else _roster_for_issue(ctx.spec, issue.labels)
    )
    console.print(
        f"\n[bold yellow]>>> board-loop[/bold yellow] {label} run for issue "
        f"#{issue.number} on PR #{pr_number} ([dim]{head}[/dim])"
    )
    try:
        outcome = execute_run(
            ctx=ctx,
            rendered_prompt=prompt,
            prompt_template_id=label,
            trigger="ci_failure" if kind == "ci_fix" else "rework",
            focus=f"{label}:#{issue.number}",
            base_ref=base_ref,
            acceptance_criteria=acceptance,
            max_iterations=params.max_iterations,
            max_turns=params.max_turns,
            eval_suite=params.eval_suite,
            model=params.model,
            issue_number=issue.number,
            pr_number=pr_number,
            dry_run=False,
            claude_bin=params.claude_bin,
            multi_agent=params.multi_agent,
            agents=roster,
            create_pr=False,
            pr_base=params.pr_base,
            extra_labels=[],
            pr_draft=False,
            auto_merge=False,
            announce=False,
            store=store,
        )
    except Exception as exc:
        err_console.print(f"[red]error[/red] in {label} run for #{issue.number}: {exc}")
        return _PassResult(summaries=[row("error", str(exc))], bailed=True,
                           dispatched=0, skipped=0)

    result = outcome.result
    _intake_run_findings(ctx, store, issue, outcome)
    if result.decision == "pr_created" and result.branch:
        try:
            code_host.push_branch(branch=result.branch, target=head)
        except CodeHostError as exc:
            err_console.print(f"[red]push failed[/red] for PR #{pr_number}: {exc}")
            return _PassResult(summaries=[row("push_failed", str(exc))], bailed=False,
                               dispatched=1, skipped=0)
        console.print(f"  [green]pushed[/green] {label} → {head} (PR #{pr_number})")
    bailed = result.decision in {"cost_cap_hit", "guardrail_blocked", "error"}
    return _PassResult(
        summaries=[row(f"{label}:{result.decision}", url=f"#{pr_number}")],
        bailed=bailed,
        dispatched=1,
        skipped=0,
    )


def _pr_observation(ctx: ForgeContext, pr_number: int):
    """(Head-Commit-Zeitstempel, CI-Status, PR-State, Mergeable) vom Code-Host.

    Fail-open: jeder Code-Host-Fehler → ``None`` (Signal unbekannt → altes
    Verhalten), damit ein Schluckauf den Tick nicht wedged."""
    host = ctx.get_code_host()
    head = host.head_committed_at(pr_number)
    try:
        meta = host.fetch_metadata(pr_number)
        ci, state, mergeable = meta.ci_status, meta.state, meta.mergeable
    except CodeHostError:
        ci, state, mergeable = None, None, None
    return head, ci, state, mergeable


def _record_observed_merge(
    ctx: ForgeContext, store: Any, events: list, pr_number: int, session_id: str
) -> Any | None:
    """Trägt einen beim Code-Host beobachteten, aber noch nicht als Event
    erfassten Merge als ``PRMerged`` nach (Mensch hat gemergt, Auto-Merge, kein
    Webhook — z.B. Azure DevOps). Damit bleibt der Event-Strom die einzige
    Wahrheit für ``qa → release``, unabhängig vom Anbieter. Idempotent: nur
    wenn noch kein ``PRMerged`` für ``pr_number`` existiert."""
    if any(
        e.kind == EventKind.PR_MERGED and (e.payload or {}).get("pr_number") == pr_number
        for e in events
    ):
        return None
    created = [
        e.ts
        for e in events
        if e.kind == EventKind.PR_CREATED and (e.payload or {}).get("pr_number") == pr_number
    ]
    ttm = int((datetime.now(UTC) - min(created)).total_seconds()) if created else 0
    evt = build_event(
        kind=EventKind.PR_MERGED,
        run_id=session_id,
        project=ctx.spec.name,
        project_fingerprint=ctx.project_fingerprint,
        factory_version=ctx.factory_version,
        spec_version=ctx.spec.spec_version,
        payload=PRMergedPayload(
            pr_number=pr_number, merger="external", time_to_merge_s=max(ttm, 0)
        ),
    )
    store.append(evt)
    return evt


def _dispatch_sync(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    pr_number: int,
    params: _DispatchParams,
    store: EventStore | None = None,
) -> _PassResult:
    """G: konfliktbehafteten PR nach einem Geschwister-Merge nachziehen.

    Erst deterministisch (``git merge`` der Basis, sauber → Fast-Forward-Push,
    kein LLM). Nur bei echten Konflikten ein Agent-Run (``conflict``) ab dem
    lokalen Konflikt-Commit."""
    res = sync_branch(ctx, pr_number)
    row = _LoopSummaryRow(issue.number, issue.title, f"sync:{res.status}", f"#{pr_number}",
                          "-", res.detail or None)
    if res.status == "synced":
        console.print(f"  [green]synced[/green] PR #{pr_number} with its base")
        return _PassResult(summaries=[row], bailed=False, dispatched=0, skipped=0)
    if res.status != "conflict" or res.conflict_ref is None:
        return _PassResult(summaries=[row], bailed=False, dispatched=0, skipped=1)
    context = "Conflicting files:\n" + "\n".join(f"- {f}" for f in res.files)
    return _dispatch_branch_run(
        ctx=ctx, issue=issue, pr_number=pr_number, params=params, kind="conflict",
        context=context, store=store, base_ref_override=res.conflict_ref,
    )


def _review_release_pr(
    ctx: ForgeContext, pr_number: int, params: _DispatchParams, store: EventStore
) -> None:
    """L3: Release-PR durch denselben Agent-Review + Merge-Gates wie jeder PR."""
    from forge_execute.agents import ClaudeCodeCLIAgent

    agent = ClaudeCodeCLIAgent(default_model=params.model, claude_bin=params.claude_bin)
    execute_pr_review(
        ctx,
        pr_number=pr_number,
        agent=agent,
        merge=True,
        model=params.model,
        issue_body=(
            "Release pull request prepared by forge. It may ONLY change the changelog "
            "and version files; approve if the changelog matches the listed changes."
        ),
        store=store,
    )


def _ci_fix_roster(spec) -> list[str] | None:
    """Roster für CI-Fix-Runs: ``triggers.on_ci_failure.agents`` (Default
    ``["developer"]``), sonst der execute_run-Default."""
    cfg = getattr(getattr(spec, "triggers", None), "on_ci_failure", None)
    return list(cfg.agents) if cfg is not None else ["developer"]


_EPIC_PREAMBLE = (
    "This is an EPIC-DECOMPOSITION task, not an implementation task. Do NOT write "
    "or change any code. Split the epic below into small, independently shippable "
    "work items (each doable in one pull request), with testable acceptance "
    "criteria in each body, explicit dependencies between them, and the file globs "
    "each one will likely touch (items with overlapping `touches` cannot run in "
    "parallel — keep them disjoint where you can). Return them ONLY in the "
    "work-items block:\n\n"
)


def _dispatch_epic_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    params: _DispatchParams,
    store: EventStore | None = None,
) -> _PassResult:
    """Dispatcht die **Epic-Zerlegung** (Roadmap A2): ein Planner-Run liefert
    Kind-Items im ``FORGE-WORKITEMS``-Block; ``intake`` legt sie mit
    ``parent`` = Epic an (``WorkItemCreated``) → ``has_decomposition`` →
    ``epic → tracking``. Kinder eines vom Menschen in ``forge:epic``
    gesetzten Epics gelten als freigegeben (starten in ``requirements``)."""
    from forge_execute.agents.templates import WORKITEMS_FORMAT_HINT

    roster = _roster_for_issue(ctx.spec, issue.labels) or ["architect"]
    prompt = (
        _EPIC_PREAMBLE
        + "```\n" + WORKITEMS_FORMAT_HINT + "\n```\n\n"
        + wrap_issue_body(title=issue.title, body=issue.body)
    )
    console.print(
        f"\n[bold magenta]>>> board-loop[/bold magenta] epic decomposition for "
        f"#{issue.number} [italic]{issue.title}[/italic]"
    )
    try:
        outcome = execute_run(
            ctx=ctx,
            rendered_prompt=prompt,
            prompt_template_id="epic",
            trigger="issue_label",
            focus=f"epic:#{issue.number}",
            base_ref=params.base_ref,
            acceptance_criteria=None,
            max_iterations=1,
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
            store=store,
        )
    except Exception as exc:
        err_console.print(f"[red]error[/red] in epic run for #{issue.number}: {exc}")
        return _PassResult(
            summaries=[_LoopSummaryRow(issue.number, issue.title, "error", None, "-", str(exc))],
            bailed=True, dispatched=0, skipped=0,
        )
    blocks = list(outcome.result.workitems_blocks or [])
    created: list[int] = []
    extra: tuple[str, ...] = ()
    # G/E13: alle Kinder landen in forge/epic-<N>, ausgeliefert wird als Ganzes.
    if (
        blocks
        and parse_integration_mode(issue.body) == "branch"
        and ensure_integration_branch(ctx, issue.number, params.pr_base)
    ):
        extra = (f"Integration-Branch: {integration_branch_name(issue.number)}",)
    if blocks:
        with _store_scope(ctx, store) as s:
            created = intake_blocks(
                ctx, store=s, run_id=outcome.result.run_id, blocks=blocks,
                source="epic_decomposition", parent=issue.number, parent_approved=True,
                extra_lines=extra,
            ).created
    return _PassResult(
        summaries=[_LoopSummaryRow(
            issue.number, issue.title,
            f"epic:{len(created)} items" if created else "epic:no_items",
            None, "-", None,
        )],
        bailed=outcome.result.decision in {"cost_cap_hit", "guardrail_blocked", "error"},
        dispatched=1, skipped=0,
    )


def _dispatch_resume(
    *,
    ctx: ForgeContext,
    order: ResumeOrder,
    params: _DispatchParams,
    issue: ReadyIssue | None,
    store: EventStore | None = None,
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
            store=store,
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
                    parallel_running=result.parallel_running,
                    capacity=result.capacity,
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
    tracker: WorkTracker | None = None,
    max_issues: int,
    interval_s: float,
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
    max_ticks: int | None = None,
) -> HeartbeatStats:
    """Flacher Dauerbetrieb (Phase B): pollt board-ready Issues und arbeitet
    sie ab — ohne Stage-State-Machine."""
    tracker = tracker or ctx.get_tracker()

    def make_tick(_store: Any, _session_id: str) -> Callable[[int], TickResult]:
        def tick_fn(tick_index: int) -> TickResult:
            try:
                ready = tracker.list_ready_items(ctx.spec.board)[:max_issues]
            except TrackerError as exc:
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


def _dispatch_review_run(
    *,
    ctx: ForgeContext,
    issue: ReadyIssue,
    pr_number: int,
    params: _DispatchParams,
    store: EventStore | None = None,
) -> _PassResult:
    """Dispatcht den **QA-Stage**-Run: Agent reviewed den offenen PR + merged opt-in.

    Anders als design/in-dev produziert dieser Run keinen Plan/PR, sondern ein
    ``PRReviewed`` (+ ggf. ``PRMerged``). Bei Merge leitet ``derive_signals`` im
    nächsten Tick ``has_merged_pr`` ab → ``advance`` schreibt ``qa→release`` fort.
    Bei ``request_changes`` setzt ``review_done`` weitere QA-Dispatches aus, bis
    neue Commits/ein neuer PR den Review-Stand zurücksetzen.

    Der Merge bleibt durch ``capabilities.merge_pr`` + Score-Schwelle + grünen
    CI gegated (``execute_pr_review``/``decide_merge``) — der board-loop entscheidet
    das nicht selbst.
    """
    from forge_execute.agents import ClaudeCodeCLIAgent
    from forge_execute.agents.templates import extract_workitems_block

    acceptance = with_refined_spec(
        f"Issue #{issue.number} — {issue.title}\n\n{issue.body or ''}",
        _spec_for(ctx, store, issue.number),
    )
    console.print(
        f"\n[bold magenta]>>> board-loop[/bold magenta] qa review for issue "
        f"#{issue.number} (PR #{pr_number}) [italic]{issue.title}[/italic]"
    )
    try:
        agent = ClaudeCodeCLIAgent(default_model=params.model, claude_bin=params.claude_bin)
        outcome = execute_pr_review(
            ctx,
            pr_number=pr_number,
            agent=agent,
            merge=True,
            model=params.model,
            issue_body=acceptance,
            store=store,
        )
    except Exception as exc:
        err_console.print(
            f"[red]error[/red] in qa review for issue #{issue.number}: {exc}"
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

    # A2: nicht-blockierende Folgeaufgaben aus dem Review → Items.
    followups = extract_workitems_block(outcome.reasoning or "")
    if followups:
        with _store_scope(ctx, store) as s:
            intake_blocks(
                ctx, store=s, run_id=str(ULID()), blocks=[followups],
                source="review_followup", origin_issue=issue.number,
            )
    merge_note = "merged" if outcome.merged else (outcome.merge_decision.reason or "no-merge")
    console.print(
        f"  [cyan]#{issue.number}[/cyan] review: {outcome.verdict} "
        f"(score {outcome.score:.2f}, ci {outcome.ci_status}) → {merge_note}"
    )
    return _PassResult(
        summaries=[
            _LoopSummaryRow(
                issue_number=issue.number,
                issue_title=issue.title,
                decision=f"review:{outcome.verdict}",
                pr_url=f"#{pr_number}",
                auto_merge="merged" if outcome.merged else "-",
                error=outcome.merge_error,
            )
        ],
        bailed=False,
        dispatched=1,
        skipped=0,
    )


def _run_conductor_watch(
    *,
    ctx: ForgeContext,
    tracker: WorkTracker | None = None,
    max_issues: int,
    interval_s: float,
    params: _DispatchParams,
    triager: IssueTriager | None,
    capabilities: Capabilities | None,
    max_ticks: int | None = None,
    max_parallel: int = 1,
) -> HeartbeatStats:
    """Conductor-Dauerbetrieb (Phase C): fährt die Stage-State-Machine.

    Pro Tick: alle ``forge:``-Stage-Issues laden, ``WorkItem``-Liste bauen
    (Stage aus Labels, Deps aus Body, Signale aus dem Event-Strom), Tick planen
    und effektieren — Label-Übergänge via gh, Dispatch über den bestehenden
    ``execute_run``-Pfad. Übergänge und Blockaden werden als
    ``WorkItemStageChanged``/``WorkItemBlocked`` persistiert.

    ``max_parallel`` = Conductor-Kapazität: bis zu N Items werden pro Tick
    dispatcht. Bei ``>1`` laufen sie nebenläufig in einem ThreadPool, jeweils im
    eigenen Worktree; alle Event-Writes teilen sich die EINE (RLock-serialisierte)
    EventStore-Connection des Heartbeats. Default ``1`` = exakt das bisherige
    sequenzielle Verhalten.
    """
    stage_labels = [s.value for s in Stage]
    tracker = tracker or ctx.get_tracker()

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
                issues = tracker.list_stage_items(
                    stage_labels=stage_labels, state="all"
                )
            except TrackerError as exc:
                err_console.print(
                    f"[red]board error[/red] (tick {tick_index}): {exc}"
                )
                return TickResult()

            # A2: deterministische Quellen (CI rot auf main, Schedules) zuerst —
            # neu angelegte Items erscheinen ab dem nächsten Tick im Board.
            try:
                tick_sources(ctx, store=store, session_id=session_id, now=datetime.now(UTC))
            except Exception as exc:  # Quellen dürfen den Tick nie killen
                err_console.print(f"[yellow]intake sources failed[/yellow]: {exc}")

            events = []
            for kind in (
                EventKind.RUN_STARTED,
                EventKind.RUN_FINISHED,
                EventKind.REQUIREMENTS_REFINED,
                EventKind.PLAN_PROPOSED,
                EventKind.PR_CREATED,
                EventKind.PR_REVIEWED,
                EventKind.PR_MERGED,
                EventKind.RELEASE_TAGGED,
                EventKind.RUN_RESUME_SCHEDULED,
                EventKind.WORK_ITEM_CREATED,
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
                    store=store,
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
                # Re-Review-Gate (A2): nur QA-Items mit offenem PR brauchen den
                # Head-Commit-Zeitstempel, damit ein nachgebesserter
                # request_changes-PR erneut reviewt wird. Eine gh-Call pro
                # QA-Item (nicht pro Issue); fail-open → None bei jedem Fehler.
                head_committed_at = None
                ci_status = None
                mergeable = None
                if stage in (Stage.QA, Stage.IN_DEV, Stage.TRACKING):
                    qa_pr = pr_number_for_issue(events, issue.number)
                    if qa_pr is not None:
                        head_committed_at, ci_status, pr_state, mergeable = (
                            _pr_observation(ctx, qa_pr)
                        )
                        if pr_state == "MERGED":
                            merged_evt = _record_observed_merge(
                                ctx, store, events, qa_pr, session_id
                            )
                            if merged_evt is not None:
                                events.append(merged_evt)
                if stage == Stage.REQUIREMENTS:
                    # A1: Merge des Spec-PRs (meist durch einen Menschen) als
                    # PRMerged nachtragen → spec_pending fällt → design.
                    spec_pr = spec_pr_for_issue(events, issue.number)
                    if spec_pr is not None:
                        _, _, spec_state, _ = _pr_observation(ctx, spec_pr)
                        if spec_state == "MERGED":
                            merged_evt = _record_observed_merge(
                                ctx, store, events, spec_pr, session_id
                            )
                            if merged_evt is not None:
                                events.append(merged_evt)
                signals = derive_signals(
                    events,
                    issue.number,
                    head_committed_at=head_committed_at,
                    ci_status=ci_status,
                    mergeable=mergeable,
                )
                if stage == Stage.RELEASE and ctx.spec.release.mode == "train":
                    signals = replace(signals, release_batched=True)
                if stage == Stage.TRACKING:
                    children = epic_children(events, issue.number)
                    stages_by_number = {i.number: stage_of(i.labels) for i in issues}
                    all_done = bool(children) and all(
                        stages_by_number.get(c) == Stage.DONE for c in children
                    )
                    if parse_integration_mode(issue.body) == "branch":
                        # G: alle Kinder im Integrations-Branch → Sammel-PR
                        # öffnen (einmal); tracking → qa folgt über has_open_pr.
                        if all_done and not signals.has_open_pr:
                            run_id = epic_run_id(events, issue.number)
                            if run_id and open_integration_pr(
                                ctx, epic=issue, run_id=run_id, store=store,
                                base=params.pr_base,
                            ):
                                events.extend(
                                    e for e in store.events_by_kind(EventKind.PR_CREATED)
                                    if e.run_id == run_id
                                )
                                signals = derive_signals(events, issue.number)
                    else:
                        signals = replace(signals, children_done=all_done)
                # A1: in-dev-Item, dessen Dev-Run keinen PR produzierte →
                # Re-Dispatch-/Eskalations-Signale aus dem Event-Strom ableiten.
                if stage == Stage.IN_DEV:
                    failed, attempts = derive_dev_failure(events, issue.number)
                    signals = replace(
                        signals, dev_failed_no_pr=failed, dev_attempts=attempts
                    )
                items.append(
                    WorkItem(
                        number=issue.number,
                        stage=stage,
                        depends_on=tuple(parse_depends_on(issue.body)),
                        touches=tuple(parse_touches(issue.body)),
                        signals=signals,
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
                tracker.set_stage(
                    number=t.number,
                    add=t.to_stage.value,
                    remove=t.from_stage.value,
                )
                _emit_stage_changed(t)
                console.print(
                    f"  [cyan]#{t.number}[/cyan] {t.from_stage.value} → "
                    f"{t.to_stage.value} ([dim]{t.reason}[/dim])"
                )

            def _run_order(order: DispatchOrder) -> _PassResult | None:
                """Effektiert EINEN DispatchOrder (Team nach Stage). Thread-safe:
                Worktree pro Run isoliert, Event-Writes über den geteilten,
                RLock-serialisierten ``store``.

                Fängt jede Exception ab und gibt sie als ``bailed``-Result
                zurück — ein einzelner kaputter Run darf den parallelen Tick
                (``pool.map`` re-raised sonst) und damit den Heartbeat nicht
                killen."""
                try:
                    issue = by_number.get(order.number)
                    if issue is None:
                        return None
                    # requirements → architect-Run (Akzeptanzkriterien, kein PR);
                    # design → architect-Run (Plan, kein PR); qa → Review-Merge-Agent
                    # (PRReviewed/PRMerged, kein neuer PR); release → deterministischer
                    # Tag/Release-Effekt (kein LLM-Run); sonst → Dev-Loop (PR).
                    if order.stage == Stage.REQUIREMENTS:
                        return _dispatch_requirements_run(
                            ctx=ctx, issue=issue, params=params, store=store
                        )
                    if order.stage == Stage.RELEASE:
                        return _dispatch_release_run(
                            ctx=ctx, issue=issue, store=store, session_id=session_id
                        )
                    if order.stage == Stage.EPIC:
                        return _dispatch_epic_run(
                            ctx=ctx, issue=issue, params=params, store=store
                        )
                    if order.stage == Stage.DESIGN:
                        return _dispatch_design_run(
                            ctx=ctx, issue=issue, params=params, store=store
                        )
                    if order.kind == "sync":
                        pr_num = pr_number_for_issue(events, order.number)
                        if pr_num is None:
                            return None
                        return _dispatch_sync(
                            ctx=ctx, issue=issue, pr_number=pr_num, params=params, store=store
                        )
                    if order.kind in ("rework", "ci_fix"):
                        pr_num = pr_number_for_issue(events, order.number)
                        if pr_num is None:
                            return None
                        context = (
                            _latest_review_reasoning(ctx, events, pr_num)
                            if order.kind == "rework"
                            else ctx.get_code_host().ci_failure_summary(pr_num)
                        )
                        return _dispatch_branch_run(
                            ctx=ctx, issue=issue, pr_number=pr_num, params=params,
                            kind=order.kind, context=context, store=store,
                        )
                    if order.stage == Stage.QA:
                        pr_num = pr_number_for_issue(events, order.number)
                        if pr_num is None:
                            err_console.print(
                                f"[yellow]skip[/yellow] qa #{order.number}: kein offener PR gefunden"
                            )
                            return None
                        return _dispatch_review_run(
                            ctx=ctx, issue=issue, pr_number=pr_num, params=params, store=store
                        )
                    return _dispatch_issues(
                        ctx=ctx,
                        issues=[issue],
                        params=params,
                        triager=triager,
                        capabilities=capabilities,
                        store=store,
                    )
                except Exception as exc:
                    err_console.print(
                        f"[red]error[/red] dispatching #{order.number} "
                        f"({order.stage.value}): {exc}"
                    )
                    return _PassResult(
                        summaries=[], bailed=True, dispatched=0, skipped=0
                    )

            # Bei capacity>1 werden die Dispatch-Orders nebenläufig effektiert.
            # run_conductor_tick ruft set_stage (alle Übergänge) VOR dispatch —
            # die Reihenfolge (Label erst, dann Run) bleibt also gewahrt; nur die
            # Run-Effekte selbst überlappen. Ergebnisse werden nach dem Tick
            # eingesammelt und seriell ausgegeben.
            pending: list[DispatchOrder] = []

            def dispatch(order: DispatchOrder) -> None:
                pending.append(order)

            capacity = tick_capacity(ctx, store, max_parallel)
            result = run_conductor_tick(
                items=items,
                capacity=capacity,
                set_stage=set_stage,
                dispatch=dispatch,
                on_blocked=_emit_blocked,
            )

            # L3: Release-Train — ein Schritt pro Tick über alle release-Items.
            if ctx.spec.release.mode == "train":
                waiting = [
                    w.number for w in items
                    if w.stage == Stage.RELEASE and not w.signals.release_done
                ]
                try:
                    train = train_tick(
                        ctx, store=store, session_id=session_id, events=events,
                        waiting_items=waiting, base=params.pr_base, now=datetime.now(UTC),
                        review_fn=lambda n: _review_release_pr(ctx, n, params, store),
                    )
                    if train.action not in ("idle",):
                        console.print(f"  [dim]release train: {train.action} {train.detail}[/dim]")
                except Exception as exc:  # der Train darf den Tick nie killen
                    err_console.print(f"[yellow]release train failed[/yellow]: {exc}")

            pass_results: list[_PassResult] = []
            if max_parallel > 1 and len(pending) > 1:
                with ThreadPoolExecutor(max_workers=max_parallel) as pool:
                    for res in pool.map(_run_order, pending):
                        if res is not None:
                            pass_results.append(res)
            else:
                for order in pending:
                    res = _run_order(order)
                    if res is not None:
                        pass_results.append(res)

            for res in pass_results:
                _print_loop_summary(res.summaries, bailed=res.bailed)
                counters["dispatched"] += res.dispatched
                counters["bailed"] = counters["bailed"] or res.bailed

            return TickResult(
                dispatched=counters["dispatched"],
                blocked=result.blocked,
                bailed=bool(counters["bailed"]),
                scheduled_resume_count=resume_count,
                parallel_running=len(pending) if max_parallel > 1 else min(len(pending), 1),
                capacity=capacity,
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
    store: EventStore | None = None,
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
    _emit_triage_event(ctx=ctx, issue=issue, result=result, run_id=run_id, store=store)

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
                ctx.get_tracker().comment(
                    number=issue.number,
                    body=_format_triage_comment(result),
                )
            except TrackerError as exc:
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
                ctx.get_tracker().close(
                    number=issue.number,
                    reason=close_reason,
                )
            except TrackerError as exc:
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
    store: EventStore | None = None,
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
    owns_store = store is None
    store = store if store is not None else ctx.open_store()
    try:
        store.append(evt)
    finally:
        if owns_store:
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


def _print_dry_run_table(ready: list[ReadyIssue], provider: str) -> None:
    table = Table(
        title=f"board-loop dry-run · {provider} · {len(ready)} ready",
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
