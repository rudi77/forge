"""`forge init` — legt .forge/project.yaml mit sicheren Defaults an."""

from __future__ import annotations

from pathlib import Path

import yaml

from forge_cli.runtime import console


def init_command() -> None:
    """Implementierung von `forge init`."""
    cfg = Path.cwd() / ".forge" / "project.yaml"

    if cfg.exists():
        console.print(".forge/project.yaml existiert bereits — kein Überschreiben.")
        return

    cfg.parent.mkdir(parents=True, exist_ok=True)

    config = {
        "spec_version": "0.5",
        "name": Path.cwd().name,
        "surfaces": [],
        "capabilities": {
            "merge_pr": False,
            "push_to_main": False,
        },
        "cost_caps": {
            "per_run_usd": 5.0,
        },
    }

    cfg.write_text(yaml.dump(config, allow_unicode=True), encoding="utf-8")

    console.print(f"[green]Erstellt:[/green] {cfg}")
