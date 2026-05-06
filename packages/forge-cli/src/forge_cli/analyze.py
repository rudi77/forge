"""`forge analyze` — Standard-Reports aus dem Event-Store.

Drei Reports gemäß Spec Teil 8 / 10:

1. **Run-Outcomes** — letzte N Runs mit Decision, Cost, Score-Delta
2. **Cost pro Focus** — wo Geld fließt, was es bringt
3. **PR-Merge-Rate pro Focus** — Mensch-Maschine-Match-Indikator
4. **Top Failure-Modes** — recurring Stolperfallen

Ausgabe als Markdown, default nach stdout. Mit `--output FILE` in eine
Datei.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from forge_cli.runtime import ContextError, console, err_console, load_context


def analyze_command(
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml."),
    ] = None,
    last_runs: Annotated[
        int,
        typer.Option("--last", "-n", help="Nur die letzten N Runs anzeigen."),
    ] = 20,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Markdown-Datei statt stdout."),
    ] = None,
) -> None:
    """Implementierung von `forge analyze`."""
    try:
        ctx = load_context(spec_path=spec_path)
    except ContextError as exc:
        err_console.print(f"[red]error[/red]: {exc}")
        raise typer.Exit(code=2) from None

    store = ctx.open_store()
    try:
        markdown = _render_report(store, project=ctx.spec.name, last_runs=last_runs)
    finally:
        store.close()

    if output:
        output.write_text(markdown, encoding="utf-8")
        console.print(f"[green]wrote[/green] {output}")
    else:
        console.print(markdown)


def _render_report(store, *, project: str, last_runs: int) -> str:
    sections: list[str] = []
    sections.append(f"# forge analyze — {project}\n")
    sections.append(_section_recent_runs(store, last_runs))
    sections.append(_section_cost_per_focus(store))
    sections.append(_section_pr_merge_rate(store))
    sections.append(_section_failure_modes(store))
    return "\n".join(sections)


def _section_recent_runs(store, last: int) -> str:
    rows = store.query(
        """
        SELECT run_id, started_at, focus, decision, final_score,
               score_delta, total_cost_usd
        FROM runs_with_outcomes
        ORDER BY started_at DESC
        LIMIT ?
        """,
        [last],
    )
    if not rows:
        return "## Recent runs\n\n_no runs yet._\n"

    out = ["## Recent runs", "", "| run_id | started | focus | decision | score | Δ | cost |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        rid = r["run_id"][:10]
        ts = r["started_at"].isoformat(timespec="seconds") if r["started_at"] else "—"
        focus = r["focus"] or "—"
        decision = r["decision"] or "—"
        score = f"{r['final_score']:.3f}" if r["final_score"] is not None else "—"
        delta = (
            f"{'+' if r['score_delta'] >= 0 else ''}{r['score_delta']:.3f}"
            if r["score_delta"] is not None
            else "—"
        )
        cost = f"${r['total_cost_usd']:.3f}" if r["total_cost_usd"] is not None else "—"
        out.append(f"| `{rid}` | {ts} | {focus} | {decision} | {score} | {delta} | {cost} |")
    out.append("")
    return "\n".join(out)


def _section_cost_per_focus(store) -> str:
    rows = store.query(
        """
        SELECT focus, run_count, pr_count, total_cost_usd, mean_cost_usd
        FROM cost_per_focus
        ORDER BY total_cost_usd DESC NULLS LAST
        """
    )
    if not rows:
        return "## Cost per focus\n\n_no data._\n"

    out = ["## Cost per focus", "", "| focus | runs | PRs | total $ | avg $/run |", "|---|---|---|---|---|"]
    for r in rows:
        focus = r["focus"] or "—"
        total = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
        avg = f"${r['mean_cost_usd']:.3f}" if r["mean_cost_usd"] is not None else "—"
        out.append(
            f"| {focus} | {r['run_count']} | {r['pr_count']} | {total} | {avg} |"
        )
    out.append("")
    return "\n".join(out)


def _section_pr_merge_rate(store) -> str:
    rows = store.query(
        """
        SELECT focus, prs_created, prs_merged, merge_rate
        FROM pr_merge_rate_by_focus
        ORDER BY prs_created DESC NULLS LAST
        """
    )
    if not rows:
        return "## PR merge rate by focus\n\n_no data._\n"

    out = [
        "## PR merge rate by focus",
        "",
        "| focus | created | merged | rate |",
        "|---|---|---|---|",
    ]
    for r in rows:
        focus = r["focus"] or "—"
        rate = f"{r['merge_rate'] * 100:.0f}%" if r["merge_rate"] is not None else "—"
        out.append(f"| {focus} | {r['prs_created']} | {r['prs_merged']} | {rate} |")
    out.append("")
    return "\n".join(out)


def _section_failure_modes(store) -> str:
    rows = store.query(
        """
        SELECT kind, error_class, occurrences, last_seen
        FROM top_failure_modes
        LIMIT 10
        """
    )
    if not rows:
        return "## Top failure modes\n\n_no failures recorded._\n"

    out = [
        "## Top failure modes",
        "",
        "| event | error_class | count | last seen |",
        "|---|---|---|---|",
    ]
    for r in rows:
        last = (
            r["last_seen"].isoformat(timespec="seconds")
            if r["last_seen"] is not None
            else "—"
        )
        out.append(
            f"| {r['kind']} | {r['error_class'] or '—'} | {r['occurrences']} | {last} |"
        )
    out.append("")
    return "\n".join(out)
