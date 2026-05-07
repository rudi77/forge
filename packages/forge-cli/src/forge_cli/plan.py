"""`forge plan` — produces an architect-only plan, without code changes.

Spec v0.3 Teil 6.5 / 6.1: der architect-Subagent ist read-only und produziert
einen Plan als Markdown. `forge plan <prompt>` läuft den architect direkt und
gibt seinen Plan aus, ohne developer/tester aufzurufen.

Use-case: der Operator möchte den Plan reviewen, BEVOR Code geschrieben wird.
Output ist Markdown nach stdout oder `--output FILE`.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Annotated

import typer
from forge_execute._venv import venv_aware_env
from forge_execute.agents.templates import templates_dir

from forge_cli.runtime import ContextError, console, err_console, load_context

_IS_WINDOWS = platform.system() == "Windows"

ARCHITECT_INSTRUCTIONS = """\
You are running in `forge plan` mode — produce a plan ONLY, do not edit \
files. Even though you have Read/Glob/Grep tools listed, DO NOT modify \
anything. The operator will review your plan and start implementation in \
a separate run.

Read CLAUDE.md and .forge/project.yaml first to understand the project's \
constraints. Then read enough source files to ground your plan in reality. \
Output your plan as a Markdown document with the standard sections: Goal, \
Acceptance, Existing patterns I found, Design decisions, Subtasks, Out of \
scope, Risk.

If the task is unclear (you can't decide what 'done' means), output a \
'# Insufficient context' header instead and list the questions you need \
answered. Do not invent answers.
"""


def plan_command(
    prompt: Annotated[str, typer.Argument(help="Beschreibung der Aufgabe.")],
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Plan in Datei schreiben statt stdout."),
    ] = None,
    model: Annotated[
        str,
        typer.Option("--model", help="Claude-Modell."),
    ] = "sonnet",
    max_turns: Annotated[
        int,
        typer.Option("--max-turns", help="Max. claude-Tool-Turns für den architect."),
    ] = 20,
    claude_bin: Annotated[
        str,
        typer.Option("--claude-bin", help="Pfad zum Claude-Binary."),
    ] = "claude",
) -> None:
    """Implementierung von `forge plan`."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    plan_md = _invoke_architect(
        ctx=ctx,
        user_task=prompt,
        model=model,
        max_turns=max_turns,
        claude_bin=claude_bin,
    )

    if output is not None:
        output.write_text(plan_md, encoding="utf-8")
        console.print(f"[green]wrote plan to[/green] {output}")
    else:
        console.print(plan_md)


def _invoke_architect(
    *,
    ctx,
    user_task: str,
    model: str,
    max_turns: int,
    claude_bin: str,
) -> str:
    """Ruft claude im architect-only-Modus, Read-only-Tools."""
    architect_md = (templates_dir() / "architect.md").read_text(encoding="utf-8")
    system_prompt = architect_md + "\n\n" + ARCHITECT_INSTRUCTIONS

    cmd = [
        claude_bin,
        "-p",
        f"Plan this task: {user_task}",
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        "--permission-mode",
        "bypassPermissions",
        "--append-system-prompt",
        system_prompt,
        "--allowedTools",
        "Read,Glob,Grep",  # read-only — architect is forbidden from writing
        "--model",
        model,
        "--add-dir",
        str(ctx.repo_root),
    ]

    env = venv_aware_env(ctx.repo_root, dict(os.environ))
    popen_kwargs: dict = dict(
        cwd=str(ctx.repo_root),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    if _IS_WINDOWS:
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    console.print(
        f"[cyan]forge plan[/cyan]: invoking architect "
        f"(model={model}, max-turns={max_turns})"
    )
    try:
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except FileNotFoundError as exc:
        err_console.print(
            f"[red]error[/red]: claude binary not found ({claude_bin!r}). "
            "Install Claude Code CLI."
        )
        raise typer.Exit(code=2) from exc

    try:
        stdout, stderr = proc.communicate(timeout=900)  # 15 min wallclock
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        err_console.print("[red]error[/red]: claude exceeded 15 min wallclock")
        raise typer.Exit(code=1) from None

    try:
        raw = json.loads(stdout) if stdout.strip() else {}
    except json.JSONDecodeError:
        raw = {}

    # Soft-fail subtypes (max_turns) sind ok — claude kann trotzdem einen
    # gültigen Plan im result-Feld haben.
    if proc.returncode != 0 and raw.get("subtype") not in {
        "error_max_turns",
        "error_during_execution",
    }:
        err_console.print(
            f"[red]error[/red]: claude exited {proc.returncode}\n"
            f"stderr: {stderr.strip()[:500]}"
        )
        raise typer.Exit(code=1)

    plan_text = str(raw.get("result") or "").strip()
    if not plan_text:
        err_console.print("[red]error[/red]: claude returned empty plan")
        raise typer.Exit(code=1)
    return plan_text
