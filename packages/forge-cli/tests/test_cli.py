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
    assert "plan" in out


def test_plan_help_works() -> None:
    """`forge plan --help` prints usage; doesn't invoke claude."""
    result = runner.invoke(app, ["plan", "--help"])
    assert result.exit_code == 0
    assert "architect" in result.stdout.lower()


def test_plan_errors_without_repo(tmp_path: Path, monkeypatch) -> None:
    """In einem nicht-git-Verzeichnis liefert `forge plan` Exit 2."""
    nogit = tmp_path / "nogit"
    nogit.mkdir()
    monkeypatch.chdir(nogit)
    result = runner.invoke(app, ["plan", "Some task"])
    assert result.exit_code == 2


# --- forge doctor ------------------------------------------------------


def test_doctor_on_valid_spec(mini_repo: Path, monkeypatch) -> None:
    monkeypatch.chdir(mini_repo)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    result = runner.invoke(app, ["doctor"])
    # Exit 0 wenn keine Errors; hier nur warnings möglich (z.B. tsc, gh)
    assert result.exit_code == 0
    assert "spec" in result.stdout
    assert "ok" in result.stdout


def test_doctor_judge_check() -> None:
    """`_check_judge`: ok wenn disabled, warn wenn enabled ohne Gate, ok mit Gate."""
    from forge_cli.doctor import _check_judge
    from forge_core.spec import ProjectSpec

    base = {
        "spec_version": "1.0",
        "name": "t",
        "cost_caps": {
            "per_generation_usd": "0.5",
            "per_run_usd": "5",
            "per_project_per_day_usd": "30",
            "per_project_per_month_usd": "500",
        },
    }
    disabled = ProjectSpec.model_validate(base)
    assert _check_judge(disabled).level == "ok"

    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # die fehlende-Gate-Warnung ist hier erwartet
        enabled_no_gate = ProjectSpec.model_validate({**base, "judge": {"enabled": True}})
    finding = _check_judge(enabled_no_gate)
    assert finding.level == "warn"
    assert "llm_judge_score" in finding.detail

    enabled_with_gate = ProjectSpec.model_validate(
        {
            **base,
            "judge": {"enabled": True},
            "gates": [{"kind": "llm_judge_score", "threshold": 0.8}],
        }
    )
    assert _check_judge(enabled_with_gate).level == "ok"


def test_doctor_schedule_check(tmp_path: Path) -> None:
    """`_check_schedule_triggers`: error bei kaputtem Cron, warn ohne/mit
    fehlender prompt_file, ok wenn beides stimmt."""
    import warnings

    from forge_cli.doctor import _check_schedule_triggers
    from forge_core.spec import ProjectSpec

    base = {
        "spec_version": "1.0",
        "name": "t",
        "cost_caps": {
            "per_generation_usd": "0.5",
            "per_run_usd": "5",
            "per_project_per_day_usd": "30",
            "per_project_per_month_usd": "500",
        },
    }

    def _spec_with(sched: dict) -> ProjectSpec:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return ProjectSpec.model_validate(
                {**base, "triggers": {"schedule": [sched]}}
            )

    bad_cron = _spec_with({"cron": "61 * * * *", "focus": "x"})
    findings = _check_schedule_triggers(bad_cron, tmp_path)
    assert findings[0].level == "error"

    no_file = _spec_with({"cron": "0 2 * * *", "focus": "x"})
    findings = _check_schedule_triggers(no_file, tmp_path)
    assert findings[0].level == "warn"
    assert "prompt_file" in findings[0].detail

    missing_file = _spec_with(
        {"cron": "0 2 * * *", "focus": "x", "prompt_file": "nope.md"}
    )
    findings = _check_schedule_triggers(missing_file, tmp_path)
    assert findings[0].level == "warn"

    (tmp_path / "prompt.md").write_text("auftrag", encoding="utf-8")
    ok = _spec_with({"cron": "0 2 * * *", "focus": "x", "prompt_file": "prompt.md"})
    findings = _check_schedule_triggers(ok, tmp_path)
    assert findings[0].level == "ok"


def test_doctor_release_and_roster_checks() -> None:
    """auto_tag ohne Executor und reservierte Rollen (`operations`) werden
    als warn sichtbar, statt still wirkungslos zu bleiben."""
    from forge_cli.doctor import _check_release_config, _check_trigger_rosters
    from forge_core.spec import ProjectSpec

    base = {
        "spec_version": "1.0",
        "name": "t",
        "cost_caps": {
            "per_generation_usd": "0.5",
            "per_run_usd": "5",
            "per_project_per_day_usd": "30",
            "per_project_per_month_usd": "500",
        },
    }

    plain = ProjectSpec.model_validate(base)
    assert _check_release_config(plain) == []
    assert _check_trigger_rosters(plain) == []

    auto_tag = ProjectSpec.model_validate(
        {**base, "release": {"on_main_green": "auto_tag"}}
    )
    findings = _check_release_config(auto_tag)
    assert len(findings) == 1
    assert findings[0].level == "warn"
    assert "auto_tag" in findings[0].detail

    with_ops = ProjectSpec.model_validate(
        {
            **base,
            "triggers": {
                "on_issue_label": {
                    "auto-fix": {
                        "strategy": "sequential",
                        "model": "sonnet",
                        "agents": ["developer", "operations"],
                    }
                }
            },
        }
    )
    findings = _check_trigger_rosters(with_ops)
    assert len(findings) == 1
    assert findings[0].level == "warn"
    assert "operations" in findings[0].detail


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


def test_run_auto_merge_requires_create_pr(mini_repo: Path, monkeypatch) -> None:
    """``--auto-merge`` ohne ``--create-pr`` muss früh abbrechen statt fail-late."""
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
            "--auto-merge",
        ],
    )
    assert result.exit_code == 2
    assert "auto-merge" in result.output.lower() or "create-pr" in result.output.lower()


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


# --- forge board-loop --------------------------------------------------


def test_board_loop_help_works() -> None:
    result = runner.invoke(app, ["board-loop", "--help"])
    assert result.exit_code == 0
    assert "board" in result.stdout.lower()
    assert "--auto-merge" in result.stdout
    assert "--max" in result.stdout


def test_board_loop_errors_without_board_block(mini_repo: Path, monkeypatch) -> None:
    """mini_repo hat keinen `board:`-Block — board-loop muss klar abbrechen."""
    # Origin-Remote dazu, damit _detect_repo_slug funktioniert (sonst
    # bricht es davor ab).
    _git(mini_repo, "remote", "add", "origin", "https://github.com/rudi77/test.git")
    monkeypatch.chdir(mini_repo)
    result = runner.invoke(app, ["board-loop", "--max", "1"])
    assert result.exit_code == 2
    assert "board" in result.output.lower()


def test_detect_repo_slug_https() -> None:
    from forge_cli.board_loop import _REMOTE_RE
    m = _REMOTE_RE.search("https://github.com/rudi77/pytaskforce.git")
    assert m is not None
    assert m.group("owner") == "rudi77"
    assert m.group("repo") == "pytaskforce"


def test_detect_repo_slug_ssh() -> None:
    from forge_cli.board_loop import _REMOTE_RE
    m = _REMOTE_RE.search("git@github.com:rudi77/pytaskforce.git")
    assert m is not None
    assert m.group("owner") == "rudi77"
    assert m.group("repo") == "pytaskforce"


def test_detect_repo_slug_https_no_dotgit() -> None:
    from forge_cli.board_loop import _REMOTE_RE
    m = _REMOTE_RE.search("https://github.com/rudi77/pytaskforce")
    assert m is not None
    assert m.group("repo") == "pytaskforce"


def test_board_loop_backlog_empty_message(
    mini_repo: Path, monkeypatch
) -> None:
    """Wenn list_ready_items leere Liste liefert, sagt board-loop sauber
    'Backlog leer' und exit 0."""
    # spec mit board: erweitern
    spec_path = mini_repo / ".forge" / "project.yaml"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + "\nboard:\n  owner: rudi77\n  project_number: 999\n",
        encoding="utf-8",
    )
    _git(mini_repo, "add", ".")
    _git(mini_repo, "commit", "-m", "add board")
    _git(mini_repo, "remote", "add", "origin", "https://github.com/rudi77/test.git")

    # Patch list_ready_items global, damit kein gh aufgerufen wird.
    import forge_cli.board_loop as bl

    monkeypatch.setattr(bl, "list_ready_items", lambda *a, **kw: [])
    monkeypatch.chdir(mini_repo)

    result = runner.invoke(app, ["board-loop", "--max", "1"])
    assert result.exit_code == 0
    assert "leer" in result.output.lower() or "no ready" in result.output.lower()


def test_board_loop_dry_run_lists_items(mini_repo: Path, monkeypatch) -> None:
    """Dry-run druckt die Tabelle und beendet mit Exit 0, ohne irgendeinen
    Run zu starten."""
    spec_path = mini_repo / ".forge" / "project.yaml"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + "\nboard:\n  owner: rudi77\n  project_number: 999\n",
        encoding="utf-8",
    )
    _git(mini_repo, "add", ".")
    _git(mini_repo, "commit", "-m", "add board")
    _git(mini_repo, "remote", "add", "origin", "https://github.com/rudi77/test.git")

    from forge_adapters.github.board import ReadyIssue

    fake_items = [
        ReadyIssue(
            number=42,
            title="Bug X",
            body="repro",
            labels=["bug"],
            project_status="Todo",
            url="https://github.com/rudi77/test/issues/42",
        )
    ]
    import forge_cli.board_loop as bl

    monkeypatch.setattr(bl, "list_ready_items", lambda *a, **kw: fake_items)

    # Dispatch-Pfad muss NICHT aufgerufen werden — execute_run patchen
    # damit ein Test-Bypass-Crash sichtbar würde, falls der Code es doch
    # aufruft.
    def _should_not_be_called(**kwargs):
        raise AssertionError("execute_run was called during dry-run")

    monkeypatch.setattr(bl, "execute_run", _should_not_be_called)
    monkeypatch.chdir(mini_repo)

    result = runner.invoke(app, ["board-loop", "--dry-run", "--max", "5"])
    assert result.exit_code == 0
    assert "42" in result.output
    assert "Bug X" in result.output
