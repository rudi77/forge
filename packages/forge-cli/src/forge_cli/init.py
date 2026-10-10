"""`forge init` — legt .forge/project.yaml mit sicheren Defaults an.

Die erzeugte Spec ist **gültig** (lädt ohne Fehler, `forge doctor` läuft
direkt) und kommentiert — der Operator passt Surfaces, Eval-Suite und Board an.
"""

from __future__ import annotations

from pathlib import Path

from forge_cli.runtime import console

_TEMPLATE = """\
# forge — Projekt-Spec. Doku: docs/USER_GUIDE.md im forge-Repo.
spec_version: "1.0"
name: {name}

# Wo forge Code ändern darf (eng halten; lieber mehrere kleine Surfaces).
surfaces:
  app:
    paths: ["src/"]
    type: code

# Was forge NIE anfasst.
forbidden:
  - ".forge/**"
  - ".github/workflows/**"
  - "azure-pipelines*.yml"

# Erlaubte Aktionen. push_to_main/push_force sind immer verboten.
capabilities:
  run: ["pytest *", "python *"]
  merge_pr: false            # opt-in: Agent-Review-Merge
  create_release: false      # opt-in: Tags/Releases
  create_work_items: false   # opt-in: forge legt Issues/Work Items an
  push_to_main: false

# Wie gemessen wird.
eval_suites:
  quick:
    cmd: "python -m pytest -q --no-header --tb=no"
    budget_s: 300
    parses: pytest_json

gates:
  - {{kind: pytest_pass_rate, threshold: 1.0, source: quick}}

# Kosten-Obergrenzen (Pflicht, monoton steigend).
cost_caps:
  per_generation_usd: 0.50
  per_run_usd: 5.00
  per_project_per_day_usd: 20.00
  per_project_per_month_usd: 200.00

# Anbieter: github (Default) oder azure_devops.
# provider:
#   tracker: azure_devops
#   azure: {{organization: contoso, project: Shop, repository: shop-web}}

# Board für `forge board-loop` (GitHub: owner + project_number).
# board:
#   owner: dein-user
#   project_number: 1
#   filter_status: "Todo"
#   filter_labels: ["bug"]
"""


def render_default_spec(name: str) -> str:
    return _TEMPLATE.format(name=name)


def init_command() -> None:
    """Implementierung von `forge init`."""
    cfg = Path.cwd() / ".forge" / "project.yaml"

    if cfg.exists():
        console.print(".forge/project.yaml existiert bereits — kein Überschreiben.")
        return

    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(render_default_spec(Path.cwd().name), encoding="utf-8")

    console.print(f"[green]Erstellt:[/green] {cfg}")
    console.print("Als Nächstes: surfaces/eval_suites anpassen, dann `forge doctor`.")
