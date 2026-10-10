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
from forge_cli.main import app
from typer.testing import CliRunner

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

    runner.invoke(app, ["init"])
    # Kein Clobber: entweder sauberer Abbruch (!=0) oder no-op, aber die
    # Operator-Änderung muss erhalten bleiben.
    assert sentinel in cfg.read_text(encoding="utf-8"), (
        "forge init darf eine existierende Config nicht überschreiben"
    )


def test_forge_init_spec_is_valid_and_doctor_runs(tmp_path, monkeypatch):
    """Die erzeugte Spec lädt fehlerfrei — `forge doctor` startet ohne Traceback."""
    import subprocess

    from forge_core.spec import load_spec

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    assert runner.invoke(app, ["init"]).exit_code == 0
    spec = load_spec(tmp_path / ".forge" / "project.yaml")
    assert spec.name == tmp_path.name
    assert spec.capabilities.create_work_items is False
    result = runner.invoke(app, ["doctor"])
    assert "loaded" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_invalid_spec_is_reported_not_crashed(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.chdir(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".forge").mkdir()
    (tmp_path / ".forge" / "project.yaml").write_text("name: x\nspec_version: '1'\n")
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "invalid spec" in result.output
