"""`forge analyze` — Standard-Reports aus dem Event-Store.

Reports gemäß Spec Teil 8 / 10:

1. **Factory KPIs** — Fabrik-Executive-Summary über alle Runs: Durchsatz,
   Merge-/Keep-Rate, Kosten pro gemergtem PR, Lead-Time
2. **Throughput (per day)** — Durchsatz-Trend pro Tag
3. **Run-Outcomes** — letzte N Runs mit Decision, Cost, Score-Delta
4. **Cost pro Focus** — wo Geld fließt, was es bringt
5. **PR-Merge-Rate pro Focus** — Mensch-Maschine-Match-Indikator
6. **Top Failure-Modes** — recurring Stolperfallen
7. **Lessons learned** — kuratiertes Gedächtnis (LessonLearned-Events)

Die Factory-KPIs sind die „Software-Factory"-Sicht (Mantra 1): read-only
Aggregation über den Event-Strom, Voraussetzung für Koordinations- und
Skalierungsentscheidungen.

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
    # Fabrik-Sicht zuerst: die Executive Summary über alle Runs hinweg.
    sections.append(_section_factory_kpis(store))
    sections.append(_section_factory_throughput(store))
    sections.append(_section_recent_runs(store, last_runs))
    sections.append(_section_cost_per_focus(store))
    sections.append(_section_pr_merge_rate(store))
    sections.append(_section_failure_modes(store))
    sections.append(_section_lessons(store))
    return "\n".join(sections)


def _fmt_duration(seconds: float | None) -> str:
    """Sekunden → kompakte, menschenlesbare Dauer (—, 45s, 12m, 3.2h, 1.5d)."""
    if seconds is None:
        return "—"
    s = float(seconds)
    if s < 60:
        return f"{s:.0f}s"
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.1f}d"


def _section_factory_kpis(store) -> str:
    rows = store.query("SELECT * FROM factory_kpis")
    if not rows:
        return "## Factory KPIs\n\n_no runs yet._\n"
    r = rows[0]

    def pct(v: float | None) -> str:
        return f"{v * 100:.0f}%" if v is not None else "—"

    def money(v: float | None) -> str:
        return f"${v:.2f}" if v is not None else "—"

    keep = (
        f"{pct(r['keep_rate'])} ({r['kept']}/{r['generations']} generations)"
        if r["generations"]
        else "—"
    )
    out = [
        "## Factory KPIs",
        "",
        "| metric | value |",
        "|---|---|",
        f"| Runs total | {r['total_runs']} |",
        f"| PRs created | {r['prs_created']} |",
        f"| PRs merged | {r['prs_merged']} |",
        f"| Merge rate | {pct(r['merge_rate'])} |",
        f"| Keep rate | {keep} |",
        f"| Cost total | {money(r['total_cost_usd'])} |",
        f"| Cost / merged PR | {money(r['cost_per_merged_pr'])} |",
        f"| Mean run duration | {_fmt_duration(r['mean_run_duration_s'])} |",
        f"| Mean lead time (PR→merge) | {_fmt_duration(r['mean_lead_time_s'])} |",
        "",
    ]
    return "\n".join(out)


def _section_factory_throughput(store, *, last_days: int = 14) -> str:
    rows = store.query(
        """
        SELECT day, runs, prs_created, no_improvement, blocked,
               total_cost_usd, mean_duration_s
        FROM factory_throughput
        ORDER BY day DESC
        LIMIT ?
        """,
        [last_days],
    )
    if not rows:
        return "## Throughput (per day)\n\n_no data._\n"

    out = [
        "## Throughput (per day)",
        "",
        "| day | runs | PRs | no-impr | blocked | cost | avg dur |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        day = r["day"].isoformat() if r["day"] is not None else "—"
        cost = f"${r['total_cost_usd']:.2f}" if r["total_cost_usd"] is not None else "—"
        out.append(
            f"| {day} | {r['runs']} | {r['prs_created']} | "
            f"{r['no_improvement']} | {r['blocked']} | {cost} | "
            f"{_fmt_duration(r['mean_duration_s'])} |"
        )
    out.append("")
    return "\n".join(out)


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


def _section_lessons(store, *, limit: int = 25) -> str:
    rows = store.query(
        """
        SELECT lesson, category, issue_number, times_seen, last_seen
        FROM lessons_learned
        ORDER BY times_seen DESC, last_seen DESC
        LIMIT ?
        """,
        [limit],
    )
    if not rows:
        return "## Lessons learned\n\n_no lessons recorded yet._\n"

    out = [
        "## Lessons learned",
        "",
        "| category | lesson | issue | seen | last |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        category = r["category"] or "general"
        lesson = (r["lesson"] or "").replace("|", "\\|")
        issue = f"#{r['issue_number']}" if r["issue_number"] is not None else "—"
        last = (
            r["last_seen"].isoformat(timespec="seconds")
            if r["last_seen"] is not None
            else "—"
        )
        out.append(
            f"| {category} | {lesson} | {issue} | {r['times_seen']} | {last} |"
        )
    out.append("")
    return "\n".join(out)
