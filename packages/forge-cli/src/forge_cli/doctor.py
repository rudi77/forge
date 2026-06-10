"""`forge doctor` — Spec-/Tool-/Setup-Konsistenz-Check.

Prüft fünf Kategorien:

1. Spec lädt sauber, Validierungen halten
2. Tools aus `capabilities.run` sind im PATH
3. `claude` CLI ist verfügbar (Warning, nicht Error — Mock-Mode möglich)
4. `ANTHROPIC_API_KEY` ist gesetzt
5. `gh` CLI ist verfügbar (für PR-Erzeugung)

Liefert Exit-Code 0 wenn alle harten Checks ok sind, 1 bei mindestens
einem Fehler.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from forge_cli.runtime import ContextError, console, load_context


@dataclass
class Finding:
    category: str
    level: str  # ok | warn | error
    detail: str


def doctor_command(
    spec_path: Annotated[
        Path | None,
        typer.Option("--spec", help="Pfad zur project.yaml."),
    ] = None,
) -> None:
    """Implementierung von `forge doctor`."""
    findings: list[Finding] = []

    # Spec laden
    try:
        ctx = load_context(spec_path=spec_path)
        findings.append(Finding("spec", "ok", f"loaded {ctx.spec_path}"))
    except ContextError as exc:
        findings.append(Finding("spec", "error", str(exc)))
        _render(findings)
        raise typer.Exit(code=1) from None

    # capabilities.run Tools im PATH
    findings.extend(_check_run_tools(ctx.spec))

    # claude CLI
    findings.append(_check_binary("claude", category="agent", level_when_missing="warn"))

    # API-Key
    findings.append(_check_api_key())

    # gh CLI
    findings.append(_check_binary("gh", category="github", level_when_missing="warn"))

    # forbidden paths sanity (Spec Teil 7.4: forge selbst muss in forbidden sein)
    findings.append(_check_forge_self_protection(ctx.spec))

    # Judge-Konsistenz (Spec v0.5)
    findings.append(_check_judge(ctx.spec))

    # Schedule-Trigger: Cron parsebar + prompt_file vorhanden
    findings.extend(_check_schedule_triggers(ctx.spec, ctx.repo_root))

    # Tote Config sichtbar machen: auto_tag ohne Executor, reservierte Rollen
    findings.extend(_check_release_config(ctx.spec))
    findings.extend(_check_trigger_rosters(ctx.spec))

    has_error = any(f.level == "error" for f in findings)
    _render(findings)
    raise typer.Exit(code=1 if has_error else 0)


# --- Findings ----------------------------------------------------------


def _check_run_tools(spec) -> list[Finding]:
    """Erstes Token jedes `capabilities.run`-Patterns wird als Tool-Name
    interpretiert. Wir prüfen, ob es im PATH liegt."""
    seen: set[str] = set()
    out: list[Finding] = []
    for pattern in spec.capabilities.run:
        head = pattern.strip().split(None, 1)[0] if pattern.strip() else ""
        if not head or head in seen:
            continue
        seen.add(head)
        if shutil.which(head):
            out.append(Finding("tool", "ok", f"{head} found in PATH"))
        else:
            out.append(
                Finding(
                    "tool",
                    "warn",
                    f"{head} not in PATH — eval suite may fail",
                )
            )
    return out


def _check_binary(
    name: str,
    *,
    category: str,
    level_when_missing: str = "warn",
) -> Finding:
    if shutil.which(name):
        return Finding(category, "ok", f"{name} found in PATH")
    return Finding(category, level_when_missing, f"{name} not in PATH")


def _check_api_key() -> Finding:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return Finding("env", "ok", "ANTHROPIC_API_KEY is set")
    return Finding(
        "env",
        "error",
        "ANTHROPIC_API_KEY is not set — `forge run` (without --dry-run) will fail",
    )


def _check_forge_self_protection(spec) -> Finding:
    """Spec Teil 7.4: `.forge/**` und `.github/workflows/**` müssen verboten
    sein, damit forge nicht versehentlich ihre eigene Konfiguration ändert."""
    required = [".forge/**", ".github/workflows/**"]
    forbidden = set(spec.forbidden)
    missing = [p for p in required if p not in forbidden]
    if not missing:
        return Finding(
            "guardrail", "ok", "forbidden contains .forge/** and .github/workflows/**"
        )
    return Finding(
        "guardrail",
        "warn",
        f"forbidden missing recommended entries: {missing}",
    )


def _check_judge(spec) -> Finding:
    """Judge-Phase: wenn aktiviert, muss ein ``llm_judge_score``-Gate sie
    binden — sonst läuft der Judge (kostet Geld) ohne Wirkung auf die
    Decide-Phase."""
    if not spec.judge.enabled:
        return Finding("judge", "ok", "judge disabled (default)")
    has_gate = any(g.kind == "llm_judge_score" for g in spec.gates)
    if has_gate:
        return Finding(
            "judge",
            "ok",
            f"judge enabled, bound by llm_judge_score gate (threshold {spec.judge.threshold})",
        )
    return Finding(
        "judge",
        "warn",
        "judge.enabled but no llm_judge_score gate — judge runs but cannot "
        "block a decision; add `{kind: llm_judge_score, threshold: 0.8}` to gates",
    )


def _check_schedule_triggers(spec, repo_root: Path) -> list[Finding]:
    """Schedule-Trigger: Cron muss parsen, ``prompt_file`` muss existieren.

    Ein Trigger ohne (lesbare) Prompt-Quelle wird vom Heartbeat nie
    dispatcht — das soll der Operator VOR dem 24/7-Betrieb sehen, nicht im
    Log um 02:00.
    """
    from forge_cli.schedule import CronError, parse_cron

    out: list[Finding] = []
    for idx, sched in enumerate(spec.triggers.schedule):
        label = f"schedule[{idx}] ({sched.focus})"
        try:
            parse_cron(sched.cron)
        except CronError as exc:
            out.append(Finding("schedule", "error", f"{label}: invalid cron: {exc}"))
            continue
        if sched.prompt_file is None:
            out.append(
                Finding(
                    "schedule",
                    "warn",
                    f"{label}: no prompt_file — trigger will never dispatch",
                )
            )
        elif not (repo_root / sched.prompt_file).is_file():
            out.append(
                Finding(
                    "schedule",
                    "warn",
                    f"{label}: prompt_file {sched.prompt_file!r} not found — "
                    f"trigger will be skipped",
                )
            )
        else:
            out.append(Finding("schedule", "ok", f"{label}: cron + prompt_file ok"))
    return out


def _check_release_config(spec) -> list[Finding]:
    """`release.on_main_green: auto_tag` ist reservierte Config ohne Executor
    in v1 — still akzeptierte, wirkungslose Config ist eine Operator-Falle."""
    if spec.release.on_main_green == "auto_tag":
        return [
            Finding(
                "release",
                "warn",
                "release.on_main_green: auto_tag is configured but v1 has no "
                "tagging executor — no tag will be created on green main",
            )
        ]
    return []


def _check_trigger_rosters(spec) -> list[Finding]:
    """Roster-Einträge, für die kein Subagent-Template existiert (z.B. die
    spec-reservierte Rolle `operations`), werden beim Dispatch still
    verworfen — hier sichtbar machen."""
    from forge_execute.agents.templates import unknown_agents

    out: list[Finding] = []

    def _check(roster: list[str], where: str) -> None:
        missing = unknown_agents(list(roster))
        if missing:
            out.append(
                Finding(
                    "roster",
                    "warn",
                    f"{where}: agent(s) {missing} have no implementation and "
                    f"will be silently dropped from the roster",
                )
            )

    for lbl, cfg in spec.triggers.on_issue_label.items():
        _check(cfg.agents, f"triggers.on_issue_label[{lbl!r}]")
    if spec.triggers.on_pr_opened is not None:
        _check(spec.triggers.on_pr_opened.agents, "triggers.on_pr_opened")
    if spec.triggers.on_ci_failure is not None:
        _check(spec.triggers.on_ci_failure.agents, "triggers.on_ci_failure")
    for idx, sched in enumerate(spec.triggers.schedule):
        _check(sched.agents, f"triggers.schedule[{idx}]")
    return out


def _render(findings: list[Finding]) -> None:
    counts = {
        "ok": sum(1 for f in findings if f.level == "ok"),
        "warn": sum(1 for f in findings if f.level == "warn"),
        "error": sum(1 for f in findings if f.level == "error"),
    }
    table = Table(title="forge doctor", show_lines=False)
    table.add_column("level", style="bold", width=6)
    table.add_column("category", width=10)
    table.add_column("detail")
    for f in findings:
        color = {"ok": "green", "warn": "yellow", "error": "red"}[f.level]
        table.add_row(f"[{color}]{f.level}[/{color}]", f.category, f.detail)
    console.print(table)
    summary = (
        f"[green]{counts['ok']} ok[/green] | "
        f"[yellow]{counts['warn']} warn[/yellow] | "
        f"[red]{counts['error']} error[/red]"
    )
    console.print(summary)
