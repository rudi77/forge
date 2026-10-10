"""`forge doctor` — Spec-/Tool-/Setup-Konsistenz-Check.

Prüft fünf Kategorien:

1. Spec lädt sauber, Validierungen halten
2. Tools aus `capabilities.run` sind im PATH
3. `claude` CLI ist verfügbar (Warning, nicht Error — Mock-Mode möglich)
4. `ANTHROPIC_API_KEY` ist gesetzt
5. `gh` CLI ist verfügbar (für PR-Erzeugung) bzw. `az` bei Azure DevOps
6. optional (`--board`): alle `forge:<stage>`-Labels existieren im Tracker;
   mit `--fix` werden fehlende angelegt (Live-Verifikation, Roadmap L0)

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
    board: Annotated[
        bool,
        typer.Option(
            "--board",
            help=(
                "Tracker live prüfen: existieren alle forge:<stage>-Labels "
                "(GitHub-Labels bzw. Azure-Tags)?"
            ),
        ),
    ] = False,
    fix: Annotated[
        bool,
        typer.Option("--fix", help="Mit --board: fehlende Stage-Labels anlegen."),
    ] = False,
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

    # Anbieter-CLIs (gh für GitHub, az für Azure DevOps)
    providers = {ctx.spec.provider.tracker, ctx.spec.provider.effective_code_host}
    if "github" in providers:
        findings.append(_check_binary("gh", category="github", level_when_missing="warn"))
    if "azure_devops" in providers:
        findings.append(_check_binary("az", category="azure", level_when_missing="warn"))
        findings.append(_check_azure_pat())

    if board:
        findings.extend(_check_board_labels(ctx, fix=fix))

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
    # Kein harter Fehler: `claude` kann auch über `claude login` (Abo)
    # authentifiziert sein — dann braucht es keinen API-Key.
    return Finding(
        "env",
        "warn",
        "ANTHROPIC_API_KEY is not set — fine if `claude` is logged in "
        "(`claude login`), otherwise `forge run` will fail",
    )


def _check_azure_pat() -> Finding:
    if os.environ.get("AZURE_DEVOPS_EXT_PAT"):
        return Finding("azure", "ok", "AZURE_DEVOPS_EXT_PAT is set")
    return Finding(
        "azure",
        "warn",
        "AZURE_DEVOPS_EXT_PAT not set — az must be logged in (`az login`) instead",
    )


def required_board_labels() -> list[str]:
    """Alle Labels/Tags, die der Conductor setzt: Stages + Marker."""
    from forge_cli.stages import MARKER_LABELS, Stage

    return [s.value for s in Stage] + list(MARKER_LABELS)


def _check_board_labels(ctx, *, fix: bool) -> list[Finding]:
    """Live-Check gegen den Tracker (Roadmap L0): fehlen Stage-Labels, laufen
    ``set_stage``-Aufrufe des Conductors ins Leere (gh legt Labels beim
    ``issue edit`` nicht an)."""
    from forge_adapters.base import TrackerError

    try:
        tracker = ctx.get_tracker()
        report = tracker.ensure_labels(required_board_labels(), create=fix)
    except TrackerError as exc:
        return [Finding("board", "error", f"tracker not reachable: {exc}")]
    out = [
        Finding(
            "board",
            "ok",
            f"{tracker.provider}: {len(report.present)} forge labels present",
        )
    ]
    if report.created:
        out.append(Finding("board", "ok", f"created: {', '.join(report.created)}"))
    if report.missing:
        out.append(
            Finding(
                "board",
                "error",
                f"missing labels: {', '.join(report.missing)} — run "
                "`forge doctor --board --fix`",
            )
        )
    return out


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
