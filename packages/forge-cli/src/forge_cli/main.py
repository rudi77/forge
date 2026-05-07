"""forge — CLI-Einstiegspunkt.

Subcommands:

- `forge run`     — startet einen Sequential-Run gegen die aktuelle Spec
- `forge analyze` — Markdown-Reports aus dem Event-Store
- `forge doctor`  — Spec-Konsistenz, Tool-Verfügbarkeit, API-Keys
- `forge replay`  — rekonstruiert einen Run als Markdown-Timeline
"""

from __future__ import annotations

import typer

from forge_cli import analyze as analyze_mod
from forge_cli import doctor as doctor_mod
from forge_cli import plan as plan_mod
from forge_cli import replay as replay_mod
from forge_cli import run as run_mod

app = typer.Typer(
    name="forge",
    help="forge — messbare, replay-fähige Auto-PR-Maschine.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

app.command(
    name="run",
    help="Startet einen Sequential-Run.",
)(run_mod.run_command)

app.command(
    name="analyze",
    help="Markdown-Reports aus dem Event-Store (PR-Merge-Rate, Cost-pro-Focus, Failure-Modi).",
)(analyze_mod.analyze_command)

app.command(
    name="doctor",
    help="Prüft Spec, Tool-Verfügbarkeit, API-Key-Setup.",
)(doctor_mod.doctor_command)

app.command(
    name="replay",
    help="Rekonstruiert einen Run als lesbare Markdown-Timeline.",
)(replay_mod.replay_command)

app.command(
    name="plan",
    help="Generiert einen Plan für eine Aufgabe (architect-only, kein Code).",
)(plan_mod.plan_command)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
