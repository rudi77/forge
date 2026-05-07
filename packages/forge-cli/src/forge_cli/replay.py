"""`forge replay` — rekonstruiert einen Run als Markdown-Timeline.

Lädt alle Events des Runs und löst Artefakte aus dem Blob-Store auf. Das
Ergebnis ist eine lesbare Chronik, die der Operator zur Diagnose oder als
Korpus für spätere Replay-Tests verwendet (Spec Teil 4.4, 10.4).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from forge_core.events import EventKind
from forge_core.replay import reconstruct_run

from forge_cli.runtime import ContextError, console, err_console, load_context


def replay_command(
    run_id: Annotated[
        str,
        typer.Argument(help="Run-ID (ULID) — siehe `forge analyze`."),
    ],
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Markdown-Datei statt stdout."),
    ] = None,
    show_artifacts: Annotated[
        bool,
        typer.Option("--show-artifacts", help="Artefakt-Inhalte (Prompt, Diff) inline."),
    ] = True,
) -> None:
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    store = ctx.open_store()
    blobs = ctx.open_blobs()
    try:
        rec = reconstruct_run(run_id, store=store, blobs=blobs)
    finally:
        store.close()

    if not rec.events:
        err_console.print(f"[red]error[/red]: no events found for run_id {run_id!r}")
        raise typer.Exit(code=1)

    md = _render_timeline(rec, show_artifacts=show_artifacts)
    if output:
        output.write_text(md, encoding="utf-8")
        console.print(f"[green]wrote[/green] {output}")
    else:
        console.print(md)


def _render_timeline(rec, *, show_artifacts: bool) -> str:
    lines: list[str] = []
    lines.append(f"# Run {rec.run_id}")
    lines.append("")
    lines.append(f"- project: {rec.project}")
    lines.append(f"- factory_version: {rec.factory_version}")
    lines.append(f"- started_at: {rec.started_at}")
    lines.append(f"- finished_at: {rec.finished_at}")
    lines.append(f"- generations: {len(rec.generations)}")
    lines.append(f"- events: {len(rec.events)}")
    lines.append("")

    # Run-Level Events (RunStarted, RunFinished)
    for evt in rec.events:
        if evt.generation_id is None:
            lines.append(f"## {evt.kind.value} — {evt.ts.isoformat(timespec='seconds')}")
            lines.append("")
            lines.append(_format_payload(evt.payload))
            lines.append("")

    # Per-Generation
    for g in rec.generations:
        idx = _generation_idx(g)
        lines.append(f"## Generation {idx}")
        lines.append("")
        for evt in g.events:
            sub = f"{evt.kind.value} — {evt.ts.isoformat(timespec='seconds')}"
            lines.append(f"### {sub}")
            lines.append("")
            lines.append(_format_payload(evt.payload))
            if evt.cost_usd:
                lines.append(f"- cost: ${evt.cost_usd}")
            if evt.tokens_in or evt.tokens_out:
                lines.append(
                    f"- tokens: in={evt.tokens_in} out={evt.tokens_out}"
                )
            if evt.artifacts:
                lines.append(f"- artifacts: {sorted(evt.artifacts)}")
            lines.append("")

        if show_artifacts:
            # Plan zuerst (chronologisch + konzeptionell vor Prompt/Diff)
            if g.plan_md:
                lines.append("**plan (from architect subagent):**")
                lines.append("")
                lines.append(_truncate(g.plan_md, 6000))
                lines.append("")
            if g.proposal_prompt:
                lines.append("**prompt:**")
                lines.append("")
                lines.append("```")
                lines.append(_truncate(g.proposal_prompt, 4000))
                lines.append("```")
                lines.append("")
            if g.diff:
                lines.append("**diff:**")
                lines.append("")
                lines.append("```diff")
                lines.append(_truncate(g.diff, 6000))
                lines.append("```")
                lines.append("")

    return "\n".join(lines)


def _generation_idx(g) -> int | str:
    for evt in g.events:
        if evt.kind == EventKind.GENERATION_STARTED:
            return evt.payload.get("generation_idx", "?")
    return "?"


def _format_payload(payload: dict) -> str:
    if not payload:
        return "_(no payload)_"
    lines = []
    for key, value in payload.items():
        if isinstance(value, dict):
            inner = ", ".join(f"{k}={v}" for k, v in value.items())
            lines.append(f"- **{key}**: {{{inner}}}")
        elif isinstance(value, list):
            if len(value) == 0:
                lines.append(f"- **{key}**: []")
            elif len(value) <= 3:
                lines.append(f"- **{key}**: {value}")
            else:
                lines.append(f"- **{key}**: ({len(value)} items) {value[:3]} …")
        else:
            lines.append(f"- **{key}**: {value}")
    return "\n".join(lines)


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[:n] + f"\n… (truncated {len(text) - n} chars)"
