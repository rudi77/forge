"""`forge run` — startet einen Sequential-Run.

Drei Eingabe-Modi für den Prompt:
  --prompt-file <path>   — wörtlicher Prompt-Text
  --prompt <string>      — Prompt direkt auf der CLI
  (default)              — generischer Template-Prompt aus focus
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from forge_adapters.github import GitHubError, create_pr_for_run, render_pr_body
from forge_execute.agents import ClaudeCodeCLIAgent, MockCodingAgent
from forge_execute.runner import RunConfig, SequentialRunner
from rich.panel import Panel
from rich.table import Table

from forge_cli.runtime import ContextError, console, err_console, load_context


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

    rendered_prompt = _resolve_prompt(prompt, prompt_file, focus, ctx.spec.name)
    if rendered_prompt is None:
        err_console.print(
            "[red]error[/red]: provide --prompt, --prompt-file, or --focus to render a default prompt"
        )
        raise typer.Exit(code=2)

    if dry_run:
        console.print("[yellow]dry-run[/yellow]: using MockCodingAgent (no Claude calls)")
        agent = MockCodingAgent(callable_=lambda wt, prompt: None)
    else:
        agent = ClaudeCodeCLIAgent(
            claude_bin=claude_bin,
            default_model=model,
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
        eval_suite=eval_suite,
        model=model,
        issue_number=issue_number,
        pr_number=pr_number,
    )

    _print_pre_run_summary(config, ctx)

    store = ctx.open_store()
    blobs = ctx.open_blobs()
    pr_outcome: str | None = None
    try:
        runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
        result = runner.run()

        if create_pr and result.decision == "pr_created":
            pr_outcome = _create_pr_for_result(
                ctx=ctx,
                config=config,
                result=result,
                pr_base=pr_base,
                extra_labels=pr_label or [],
                draft=pr_draft,
                store=store,
            )
    finally:
        store.close()

    _print_post_run_summary(result, pr_outcome=pr_outcome)

    # Exit-Code: 0 wenn ein PR-würdiger Stand entstand, 1 sonst.
    raise typer.Exit(code=0 if result.decision == "pr_created" else 1)


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
    table.add_row("[bold]focus[/bold]", config.focus or "—")
    table.add_row("[bold]eval_suite[/bold]", config.eval_suite)
    table.add_row("[bold]max_iter[/bold]", str(config.max_iterations))
    table.add_row("[bold]base_ref[/bold]", config.base_ref)
    if config.model:
        table.add_row("[bold]model[/bold]", config.model)
    console.print(Panel(table, title="forge run", border_style="cyan"))


def _create_pr_for_result(
    *,
    ctx,
    config,
    result,
    pr_base: str,
    extra_labels: list[str],
    draft: bool,
    store,
) -> str:
    """Pusht den Run-Branch und erzeugt einen PR. Liefert URL oder Fehlermeldung."""
    files_changed = []
    if result.final_diff:
        from forge_execute.mutators.code import extract_changed_paths
        files_changed = extract_changed_paths(result.final_diff)

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
    )
    labels = ["forge:auto", *extra_labels]
    try:
        outcome = create_pr_for_run(
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
        return f"PR creation failed: {exc}"
    return outcome.url


def _format_pr_title(focus: str | None, score_delta: float | None) -> str:
    focus_part = focus or "auto"
    delta_part = (
        f"composite {'+' if score_delta is not None and score_delta >= 0 else ''}{score_delta:.3f}"
        if score_delta is not None
        else "composite n/a"
    )
    return f"forge: {focus_part} ({delta_part})"


def _print_post_run_summary(result, *, pr_outcome: str | None = None) -> None:
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
    if pr_outcome:
        table.add_row("[bold]PR[/bold]", pr_outcome)
    console.print(Panel(table, title="run summary", border_style=color))
