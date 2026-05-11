"""`forge run` — startet einen Sequential-Run.

Drei Eingabe-Modi für den Prompt:
  --prompt-file <path>   — wörtlicher Prompt-Text
  --prompt <string>      — Prompt direkt auf der CLI
  (default)              — generischer Template-Prompt aus focus

Der Body von ``run_command`` ist in :func:`execute_run` extrahiert, damit
``forge board-loop`` denselben Pfad ohne Typer-Layer wiederverwendet.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from forge_adapters.github import (
    GitHubError,
    create_pr_for_run,
    queue_auto_merge,
    render_pr_body,
)
from forge_execute.agents import ClaudeCodeCLIAgent, MockCodingAgent
from forge_execute.runner import RunConfig, RunResult, SequentialRunner
from rich.panel import Panel
from rich.table import Table

from forge_cli.runtime import ContextError, ForgeContext, console, err_console, load_context


@dataclass
class RunOutcome:
    """Strukturiertes Ergebnis eines Run-Aufrufs für Caller wie board-loop.

    Kapselt das, was die alte ``run_command``-Funktion früher inline
    zusammensuchte: Runner-Ergebnis, PR-Outcome (URL oder Fehlermeldung)
    und Auto-Merge-Status (wenn aktiviert).
    """

    result: RunResult
    pr_url: str | None = None
    pr_number: int | None = None
    pr_error: str | None = None
    auto_merge_queued: bool = False
    auto_merge_error: str | None = None


def run_command(
    focus: Annotated[
        str | None,
        typer.Option("--focus", "-f", help="Focus-Tag, z.B. legacy_test_revival."),
    ] = None,
    prompt: Annotated[
        str | None,
        typer.Option("--prompt", "-p", help="Prompt-Text inline."),
    ] = None,
    prompt_file: Annotated[
        Path | None,
        typer.Option("--prompt-file", help="Prompt-Text aus Datei lesen."),
    ] = None,
    prompt_template_id: Annotated[
        str,
        typer.Option(
            "--template-id",
            help="Stabile ID des Prompt-Templates (für Telemetrie).",
        ),
    ] = "manual_run_v1",
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml. Default: <repo>/.forge/project.yaml"),
    ] = None,
    trigger: Annotated[
        str,
        typer.Option(
            "--trigger",
            help="Trigger-Typ: manual | issue_label | ci_failure | schedule | pr_opened.",
        ),
    ] = "manual",
    issue_number: Annotated[
        int | None,
        typer.Option("--issue", help="Issue-Nummer (für trigger=issue_label)."),
    ] = None,
    pr_number: Annotated[
        int | None,
        typer.Option("--pr", help="PR-Nummer (für trigger=pr_opened/ci_failure)."),
    ] = None,
    max_iterations: Annotated[
        int,
        typer.Option("--max-iterations", "-n", help="Max. Anzahl Generations pro Run."),
    ] = 3,
    max_turns: Annotated[
        int,
        typer.Option(
            "--max-turns",
            help="Max. claude-Tool-Turns pro Generation. Default 8 ist knapp für Tasks mit black/isort/flake8 + pytest — bei error_max_turns auf 16+ erhöhen.",
        ),
    ] = 8,
    model: Annotated[
        str | None,
        typer.Option("--model", help="Claude-Modell (sonnet, opus). Default: aus Trigger-Config."),
    ] = None,
    eval_suite: Annotated[
        str,
        typer.Option("--eval-suite", help="Eval-Suite-Name aus der Spec."),
    ] = "quick",
    base_ref: Annotated[
        str,
        typer.Option("--base", help="Git-Ref als Worktree-Basis."),
    ] = "HEAD",
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Mock-Agent statt Claude — zum CI-Smoke-Testen."),
    ] = False,
    claude_bin: Annotated[
        str,
        typer.Option("--claude-bin", help="Pfad zum Claude-CLI-Binary."),
    ] = "claude",
    create_pr: Annotated[
        bool,
        typer.Option(
            "--create-pr",
            help="Bei decision=pr_created automatisch `gh pr create` aufrufen.",
        ),
    ] = False,
    pr_base: Annotated[
        str,
        typer.Option("--pr-base", help="Ziel-Branch für den PR (default: main)."),
    ] = "main",
    pr_label: Annotated[
        list[str] | None,
        typer.Option("--pr-label", help="Zusätzliches Label (mehrfach verwendbar)."),
    ] = None,
    pr_draft: Annotated[
        bool,
        typer.Option("--pr-draft", help="PR als Draft erzeugen."),
    ] = False,
    multi_agent: Annotated[
        bool,
        typer.Option(
            "--multi-agent",
            help=(
                "Aktiviert das Subagent-Team (architect/developer/tester). "
                "Claude Code orchestriert via Task-Tool. Erfordert größeres "
                "--max-turns (default 8 ist zu knapp)."
            ),
        ),
    ] = False,
    auto_merge: Annotated[
        bool,
        typer.Option(
            "--auto-merge",
            help=(
                "Nach `gh pr create` GitHubs Auto-Merge aktivieren "
                "(`gh pr merge --auto --squash --delete-branch`). "
                "Benötigt --create-pr. Repo muss in Settings → 'Allow "
                "auto-merge' das Feature aktiviert haben. forge selbst "
                "merged nichts — GitHub merged server-seitig wenn "
                "alle required Checks grün sind."
            ),
        ),
    ] = False,
) -> None:
    """Implementierung von `forge run`."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    valid_triggers = {"manual", "issue_label", "ci_failure", "schedule", "pr_opened"}
    if trigger not in valid_triggers:
        err_console.print(
            f"[red]error[/red]: invalid --trigger {trigger!r}; one of: {sorted(valid_triggers)}"
        )
        raise typer.Exit(code=2)

    if auto_merge and not create_pr:
        err_console.print(
            "[red]error[/red]: --auto-merge requires --create-pr"
        )
        raise typer.Exit(code=2)

    try:
        rendered_prompt = _resolve_prompt(prompt, prompt_file, focus, ctx.spec.name)
    except typer.BadParameter as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    if rendered_prompt is None:
        err_console.print(
            "[red]error[/red]: provide --prompt, --prompt-file, or --focus to render a default prompt"
        )
        raise typer.Exit(code=2)

    outcome = execute_run(
        ctx=ctx,
        rendered_prompt=rendered_prompt,
        prompt_template_id=prompt_template_id,
        trigger=trigger,
        focus=focus,
        base_ref=base_ref,
        max_iterations=max_iterations,
        max_turns=max_turns,
        eval_suite=eval_suite,
        model=model,
        issue_number=issue_number,
        pr_number=pr_number,
        dry_run=dry_run,
        claude_bin=claude_bin,
        multi_agent=multi_agent,
        create_pr=create_pr,
        pr_base=pr_base,
        extra_labels=pr_label or [],
        pr_draft=pr_draft,
        auto_merge=auto_merge,
        announce=True,
    )

    _print_post_run_summary(outcome)

    # Exit-Code: 0 wenn ein PR-würdiger Stand entstand, 1 sonst.
    raise typer.Exit(code=0 if outcome.result.decision == "pr_created" else 1)


# --- Pure-Python entry point (no Typer) -------------------------------


def execute_run(
    *,
    ctx: ForgeContext,
    rendered_prompt: str,
    prompt_template_id: str,
    trigger: str,
    focus: str | None,
    base_ref: str,
    max_iterations: int,
    max_turns: int,
    eval_suite: str,
    model: str | None,
    issue_number: int | None,
    pr_number: int | None,
    dry_run: bool,
    claude_bin: str,
    multi_agent: bool,
    create_pr: bool,
    pr_base: str,
    extra_labels: list[str],
    pr_draft: bool,
    auto_merge: bool,
    announce: bool = False,
) -> RunOutcome:
    """Wie ``run_command``, aber ohne Typer-Layer und mit RunOutcome-Return.

    Wird sowohl von ``run_command`` als auch von ``board_loop_command``
    benutzt. ``announce=True`` lässt die Pre-Run-Summary auf der Konsole
    erscheinen; im board-loop-Kontext ist das stiller (False), weil
    board_loop seine eigene Tabelle pro Iteration druckt.
    """
    if dry_run:
        if announce:
            console.print(
                "[yellow]dry-run[/yellow]: using MockCodingAgent (no Claude calls)"
            )
        agent = MockCodingAgent(callable_=lambda wt, prompt: None)
    else:
        if multi_agent and announce:
            console.print(
                "[cyan]multi-agent[/cyan]: architect -> developer -> tester via Claude Code subagents"
            )
        agent = ClaudeCodeCLIAgent(
            claude_bin=claude_bin,
            default_model=model,
            multi_agent=multi_agent,
        )

    config = RunConfig(
        spec=ctx.spec,
        project=ctx.spec.name,
        project_fingerprint=ctx.project_fingerprint,
        factory_version=ctx.factory_version,
        repo_root=ctx.repo_root,
        prompt_template_id=prompt_template_id,
        initial_prompt=rendered_prompt,
        trigger=trigger,  # type: ignore[arg-type]
        focus=focus,
        base_ref=base_ref,
        max_iterations=max_iterations,
        max_turns_per_proposal=max_turns,
        eval_suite=eval_suite,
        model=model,
        issue_number=issue_number,
        pr_number=pr_number,
    )

    if announce:
        _print_pre_run_summary(config, ctx)

    store = ctx.open_store()
    blobs = ctx.open_blobs()
    outcome = RunOutcome(result=None)  # type: ignore[arg-type]
    try:
        runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
        outcome.result = runner.run()

        if create_pr and outcome.result.decision == "pr_created":
            _create_pr_into_outcome(
                ctx=ctx,
                config=config,
                result=outcome.result,
                pr_base=pr_base,
                extra_labels=extra_labels,
                draft=pr_draft,
                store=store,
                outcome=outcome,
            )

            if auto_merge and outcome.pr_number is not None:
                try:
                    queue_auto_merge(repo=ctx.repo_root, pr_number=outcome.pr_number)
                    outcome.auto_merge_queued = True
                except GitHubError as exc:
                    outcome.auto_merge_error = str(exc)
    finally:
        store.close()

    return outcome


# --- Helpers ----------------------------------------------------------


def _resolve_prompt(
    prompt: str | None,
    prompt_file: Path | None,
    focus: str | None,
    project: str,
) -> str | None:
    if prompt is not None and prompt_file is not None:
        raise typer.BadParameter("--prompt and --prompt-file are mutually exclusive")
    if prompt is not None:
        return prompt
    if prompt_file is not None:
        return prompt_file.read_text(encoding="utf-8")
    if focus is not None:
        return _default_prompt_for_focus(focus, project)
    return None


_FOCUS_PROMPTS: dict[str, str] = {
    "legacy_test_revival": (
        "Du arbeitest in {project}. Ziel: einen oder mehrere fehlschlagende Tests "
        "wieder grün bekommen, ohne dass du andere Tests rot machst und ohne dass "
        "du die fachliche Bedeutung der Tests veränderst.\n\n"
        "1. Lies zuerst, welche Tests gerade rot sind.\n"
        "2. Wähle einen, dessen Fix lokalisiert ist (eine Datei, kleines Refactor).\n"
        "3. Implementiere den Fix.\n"
        "4. Stelle sicher, dass der Test jetzt grün ist.\n"
        "5. Stelle sicher, dass du keinen anderen Test rot gemacht hast.\n"
    ),
    "lint_cleanup": (
        "Du arbeitest in {project}. Ziel: Lint-Warnings reduzieren, ohne "
        "Verhalten zu ändern.\n\n"
        "1. Identifiziere die häufigsten Warning-Klassen.\n"
        "2. Wähle eine Klasse, deren Fix mechanisch ist.\n"
        "3. Wende den Fix konsistent an.\n"
        "4. Verifiziere via Lint, dass die Warnings weg sind und keine neuen entstanden.\n"
    ),
    "type_errors_reduction": (
        "Du arbeitest in {project}. Ziel: Type-Errors reduzieren, ohne API-Verträge "
        "zu brechen.\n\n"
        "1. Identifiziere Files mit den meisten Errors.\n"
        "2. Füge Type-Annotationen oder kleine Refactors hinzu.\n"
        "3. Verifiziere via mypy/tsc.\n"
    ),
}


def _default_prompt_for_focus(focus: str, project: str) -> str:
    template = _FOCUS_PROMPTS.get(
        focus,
        "Du arbeitest in {project}. Focus: {focus}. "
        "Mach den kleinstmöglichen Schritt, der die Surface-Constraints aus "
        "der project.yaml respektiert.",
    )
    return template.format(project=project, focus=focus)


def _print_pre_run_summary(config: RunConfig, ctx) -> None:
    table = Table.grid(padding=(0, 1))
    table.add_row("[bold]project[/bold]", config.project)
    table.add_row("[bold]factory[/bold]", ctx.factory_version)
    table.add_row("[bold]trigger[/bold]", config.trigger)
    table.add_row("[bold]focus[/bold]", config.focus or "-")
    table.add_row("[bold]eval_suite[/bold]", config.eval_suite)
    table.add_row("[bold]max_iter[/bold]", str(config.max_iterations))
    table.add_row("[bold]base_ref[/bold]", config.base_ref)
    if config.model:
        table.add_row("[bold]model[/bold]", config.model)
    console.print(Panel(table, title="forge run", border_style="cyan"))


def _create_pr_into_outcome(
    *,
    ctx: ForgeContext,
    config: RunConfig,
    result: RunResult,
    pr_base: str,
    extra_labels: list[str],
    draft: bool,
    store,
    outcome: RunOutcome,
) -> None:
    """Pusht den Run-Branch + erzeugt PR; schreibt URL/Number/Error in ``outcome``.

    Ersetzt das ältere ``_create_pr_for_result``-Helper, das nur einen
    String zurückgab. Der ``RunOutcome``-basierte Pfad lässt sich von
    ``board_loop`` mitbenutzen.
    """
    files_changed = []
    if result.final_diff:
        from forge_execute.mutators.code import extract_changed_paths
        files_changed = extract_changed_paths(result.final_diff)

    plan_md = _load_latest_plan(store, ctx, run_id=result.run_id)

    title = _format_pr_title(config.focus, result.score_delta)
    body = render_pr_body(
        run_id=result.run_id,
        focus=config.focus,
        decision=result.decision,
        final_score=result.final_score,
        score_delta=result.score_delta,
        total_cost_usd=result.total_cost_usd,
        files_changed=files_changed,
        generations_count=len(result.generations),
        factory_version=ctx.factory_version,
        diff_excerpt=result.final_diff,
        plan_md=plan_md,
    )
    labels = ["forge:auto", *extra_labels]
    try:
        pr = create_pr_for_run(
            repo=ctx.repo_root,
            branch=result.branch,
            title=title,
            body=body,
            base=pr_base,
            labels=labels,
            draft=draft,
            store=store,
            run_id=result.run_id,
            project=config.project,
            project_fingerprint=config.project_fingerprint,
            factory_version=config.factory_version,
            spec_version=ctx.spec.spec_version,
        )
    except GitHubError as exc:
        outcome.pr_error = f"PR creation failed: {exc}"
        return
    outcome.pr_url = pr.url
    outcome.pr_number = pr.pr_number


def _load_latest_plan(store, ctx: ForgeContext, *, run_id: str) -> str | None:
    """Lädt den jüngsten ``PlanProposed``-Plan-Markdown dieses Runs.

    Pläne werden pro Generation emittiert; für den PR-Body interessiert
    der letzte (der zur KEEP-Generation gehört, deren Diff in den PR
    fließt). Bei fehlendem Plan, fehlendem Blob oder Lesefehler wird
    None zurückgegeben — kein Plan im PR-Body ist sauberer als
    halbgar geladene Reste.
    """
    from forge_core.events import EventKind

    try:
        events = store.events_for_run(run_id)
    except Exception:
        return None
    plan_hash: str | None = None
    for evt in events:
        if evt.kind == EventKind.PLAN_PROPOSED:
            artifact = evt.artifacts.get("plan") if evt.artifacts else None
            if artifact:
                plan_hash = artifact
    if plan_hash is None:
        return None
    try:
        blobs = ctx.open_blobs()
        return blobs.get_text(plan_hash)
    except (FileNotFoundError, OSError):
        return None


def _format_pr_title(focus: str | None, score_delta: float | None) -> str:
    focus_part = focus or "auto"
    delta_part = (
        f"composite {'+' if score_delta is not None and score_delta >= 0 else ''}{score_delta:.3f}"
        if score_delta is not None
        else "composite n/a"
    )
    return f"forge: {focus_part} ({delta_part})"


def _print_post_run_summary(outcome: RunOutcome) -> None:
    result = outcome.result
    color = {
        "pr_created": "green",
        "no_improvement": "yellow",
        "cost_cap_hit": "red",
        "guardrail_blocked": "red",
        "preflight_blocked": "red",
        "error": "red",
    }.get(result.decision, "white")

    table = Table.grid(padding=(0, 1))
    table.add_row("[bold]run_id[/bold]", result.run_id)
    table.add_row("[bold]decision[/bold]", f"[{color}]{result.decision}[/{color}]")
    table.add_row("[bold]generations[/bold]", str(len(result.generations)))
    if result.final_score is not None:
        table.add_row("[bold]final_score[/bold]", f"{result.final_score:.4f}")
    if result.score_delta is not None:
        sign = "+" if result.score_delta >= 0 else ""
        table.add_row("[bold]delta[/bold]", f"{sign}{result.score_delta:.4f}")
    table.add_row("[bold]total_cost[/bold]", f"${result.total_cost_usd}")
    if result.branch:
        table.add_row("[bold]branch[/bold]", result.branch)
    if result.final_commit:
        table.add_row("[bold]commit[/bold]", result.final_commit[:12])
    if outcome.pr_url:
        table.add_row("[bold]PR[/bold]", outcome.pr_url)
    if outcome.pr_error:
        table.add_row("[bold]PR error[/bold]", f"[red]{outcome.pr_error}[/red]")
    if outcome.auto_merge_queued:
        table.add_row("[bold]auto-merge[/bold]", "[green]queued[/green]")
    if outcome.auto_merge_error:
        table.add_row(
            "[bold]auto-merge error[/bold]", f"[red]{outcome.auto_merge_error}[/red]"
        )
    console.print(Panel(table, title="run summary", border_style=color))
