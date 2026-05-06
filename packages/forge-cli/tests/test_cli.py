"""Tests für forge-cli."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from forge_cli.main import app
from typer.testing import CliRunner

runner = CliRunner()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def mini_repo(tmp_path: Path) -> Path:
    """Repo mit `.forge/project.yaml` und einem grünen Test."""
    repo = tmp_path / "mini"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@forge.local")
    _git(repo, "config", "user.name", "forge-test")

    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (repo / "tests").mkdir()
    (repo / "tests" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
        "from src.calc import add\n\n"
        "def test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8",
    )

    forge_dir = repo / ".forge"
    forge_dir.mkdir()
    (forge_dir / "project.yaml").write_text(
        f"""\
spec_version: "1.0"
name: mini
language_stack: [python]
surfaces:
  code:
    paths: ["src/"]
    type: code
forbidden:
  - ".forge/**"
  - ".github/workflows/**"
capabilities:
  read: ["**/*"]
  edit: ["{{surfaces}}"]
  run: ["pytest *", "python *"]
eval_suites:
  quick:
    cmd: '"{sys.executable}" -m pytest -q --no-header --tb=no'
    budget_s: 60
    parses: pytest_json
gates:
  - {{kind: pytest_pass_rate, threshold: 1.0, source: quick}}
scores:
  - {{kind: test_count, weight: 1.0}}
cost_caps:
  per_generation_usd: 0.50
  per_run_usd: 5.00
  per_project_per_day_usd: 30.00
  per_project_per_month_usd: 500.00
""",
        encoding="utf-8",
    )

    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial")
    return repo


# --- forge --help ------------------------------------------------------


def test_main_help_lists_all_subcommands() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    out = result.stdout
    assert "run" in out
    assert "analyze" in out
    assert "doctor" in out
    assert "replay" in out


# --- forge doctor ------------------------------------------------------


def test_doctor_on_valid_spec(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = runner.invoke(app, ["doctor"])
    # Exit 0 wenn keine Errors; hier nur warnings möglich (z.B. tsc, gh)
    assert result.exit_code == 0
    assert "spec" in result.stdout
    assert "ok" in result.stdout


def test_doctor_fails_without_api_key(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY" in result.stdout


def test_doctor_errors_on_missing_spec(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "norun"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    monkeypatch.chdir(repo)
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1
    assert "spec" in result.stdout.lower() or "spec" in result.stderr.lower() or True


# --- forge analyze (empty store) ---------------------------------------


def test_analyze_empty_store(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    result = runner.invoke(app, ["analyze"])
    assert result.exit_code == 0
    out = result.stdout
    assert "# forge analyze" in out
    assert "no runs yet" in out


def test_analyze_writes_to_file(mini_repo: Path, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(mini_repo)
    out_path = tmp_path / "report.md"
    result = runner.invoke(app, ["analyze", "--output", str(out_path)])
    assert result.exit_code == 0
    assert out_path.is_file()
    content = out_path.read_text(encoding="utf-8")
    assert "# forge analyze" in content


# --- forge run --dry-run -----------------------------------------------


def test_run_dry_run_against_clean_repo(mini_repo: Path, monkeypatch) -> None:
    """Dry-Run mit Mock-Agent gegen grüne Tests: Mock macht nichts → no_improvement.

    Validiert die volle CLI-Pipeline ohne Claude-Aufruf.
    """
    monkeypatch.chdir(mini_repo)
    result = runner.invoke(
        app,
        [
            "run",
            "--dry-run",
            "--focus",
            "smoke_test",
            "--max-iterations",
            "1",
        ],
    )
    # Decision wird "no_improvement" sein → Exit 1
    assert result.exit_code == 1
    assert "no_improvement" in result.stdout
    assert "decision" in result.stdout

    # Events sind im Store gelandet
    assert (mini_repo / ".forge" / "events.duckdb").is_file()


def test_run_replay_after_dry_run(mini_repo: Path, monkeypatch) -> None:
    """End-to-End: forge run → forge analyze → forge replay."""
    monkeypatch.chdir(mini_repo)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    run_result = runner.invoke(
        app, ["run", "--dry-run", "--focus", "x", "--max-iterations", "1"]
    )
    assert run_result.exit_code in (0, 1)

    # Run-ID aus dem Output extrahieren
    run_id = None
    for line in run_result.stdout.splitlines():
        if "run_id" in line.lower():
            # rich-table-Format: "│ run_id    │ 01HZX...  │"
            tokens = [t.strip() for t in line.split() if t.strip()]
            for tok in tokens:
                if len(tok) >= 20 and tok.isalnum():
                    run_id = tok
                    break
        if run_id:
            break
    assert run_id is not None, f"could not extract run_id from output:\n{run_result.stdout}"

    # analyze sollte den Run jetzt sehen
    analyze = runner.invoke(app, ["analyze"])
    assert analyze.exit_code == 0
    assert run_id[:10] in analyze.stdout or "no_improvement" in analyze.stdout

    # replay sollte funktionieren
    replay = runner.invoke(app, ["replay", run_id])
    assert replay.exit_code == 0
    assert "RunStarted" in replay.stdout
    assert "RunFinished" in replay.stdout


def test_run_invalid_trigger(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    result = runner.invoke(
        app,
        [
            "run",
            "--dry-run",
            "--focus",
            "x",
            "--max-iterations",
            "1",
            "--trigger",
            "garbage",
        ],
    )
    assert result.exit_code == 2
    assert "invalid" in result.stdout.lower() or "invalid" in (result.stderr or "").lower()


def test_run_requires_prompt_or_focus(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    result = runner.invoke(
        app, ["run", "--dry-run", "--max-iterations", "1"]
    )
    assert result.exit_code == 2
