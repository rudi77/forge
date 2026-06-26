"""Akzeptanztest für `forge init` (Dogfooding-Anker, rot→grün).

Dieser Test ist die *maschinenlesbare* Akzeptanz-Spezifikation für das Feature
"`forge init` erzeugt eine erste rudimentäre Config" (forge Mantra 1: nur was
messbar ist, zählt). Er ist ROT, solange `forge init` nicht existiert, und wird
GRÜN, sobald das Feature steht — genau der `gate_revival`-Pfad, über den forge
ein Greenfield-Feature behält.

Der Test liegt bewusst in der Forbidden Zone der Spec: das Eval-Gate führt ihn
aus, der Coding-Agent darf ihn aber nicht abschwächen. Die Implementierung muss
also echten Code liefern, nicht den Test verbiegen.
"""

from __future__ import annotations

import yaml
from typer.testing import CliRunner

from forge_cli.main import app

runner = CliRunner()


def test_forge_init_creates_rudimentary_project_yaml(tmp_path, monkeypatch):
    """`forge init` legt ein valides, rudimentäres .forge/project.yaml an."""
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0, f"forge init failed: {result.output}"

    cfg = tmp_path / ".forge" / "project.yaml"
    assert cfg.exists(), "forge init muss .forge/project.yaml anlegen"

    data = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert isinstance(data, dict), "Config muss ein YAML-Mapping sein"

    # Rudimentäre, aber für `forge doctor` tragfähige Grundstruktur.
    for key in ("spec_version", "name", "surfaces", "capabilities", "cost_caps"):
        assert key in data, f"Config fehlt Pflichtschlüssel {key!r}"

    # Sicherheits-Defaults aus v1 müssen gesetzt sein.
    assert data["capabilities"]["merge_pr"] is False
    assert data["capabilities"]["push_to_main"] is False


def test_forge_init_does_not_clobber_existing_config(tmp_path, monkeypatch):
    """Ein zweiter `forge init`-Aufruf überschreibt eine vorhandene Config nicht."""
    monkeypatch.chdir(tmp_path)

    first = runner.invoke(app, ["init"])
    assert first.exit_code == 0, first.output

    cfg = tmp_path / ".forge" / "project.yaml"
    sentinel = "# operator edit — darf nicht verloren gehen\n"
    cfg.write_text(cfg.read_text(encoding="utf-8") + sentinel, encoding="utf-8")

    second = runner.invoke(app, ["init"])
    # Kein Clobber: entweder sauberer Abbruch (!=0) oder no-op, aber die
    # Operator-Änderung muss erhalten bleiben.
    assert sentinel in cfg.read_text(encoding="utf-8"), (
        "forge init darf eine existierende Config nicht überschreiben"
    )
