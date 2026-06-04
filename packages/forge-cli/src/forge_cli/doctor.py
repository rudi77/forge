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
