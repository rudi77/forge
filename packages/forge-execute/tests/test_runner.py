"""End-to-End-Tests für SequentialRunner.

Aus todos.txt Schritt 3f: hand-trigger'ter Run gegen ein lokales Test-Repo
(absichtlich roter Test) → Test grün, committed, alle Events im Store.
Spec v0.3 Sprint 2.3: Self-Termination via Signal.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from forge_core.blobs import BlobStore
from forge_core.events import EventKind
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from forge_execute.agents import MockCodingAgent
from forge_execute.agents.base import ProposalResult
from forge_execute.runner import RunConfig, SequentialRunner


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def red_repo(tmp_path: Path) -> Path:
    """Mini-Repo mit einem absichtlich roten Test."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@forge.local")
    _git(repo, "config", "user.name", "forge-test")

    (repo / "src").mkdir()
    (repo / "src" / "__init__.py").write_text("", encoding="utf-8")
    # Bug: subtraction statt addition
    (repo / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
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
    # Realistisch wie jedes Python-Repo: pytest-Artefakte ignorieren, damit der
    # Eval-Lauf (der __pycache__/.pytest_cache erzeugt) den Worktree nicht mit
    # untracked Junk verschmutzt, den changed_files() sonst meldet.
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.coverage\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial (with red test)")
    return repo


def _spec_for_red_repo() -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "red-repo",
            "cost_caps": {
                "per_generation_usd": "0.50",
                "per_run_usd": "5.00",
                "per_project_per_day_usd": "30.00",
                "per_project_per_month_usd": "500.00",
            },
            "surfaces": {
                "code": {"paths": ["src/"], "type": "code"},
            },
            "forbidden": [],
            "capabilities": {"run": ["pytest *", "python *"]},
            "eval_suites": {
                "quick": {
                    "cmd": f'"{sys.executable}" -m pytest -q --no-header --tb=no',
                    "budget_s": 60,
                    "parses": "pytest_json",
                },
            },
            "gates": [
                {"kind": "pytest_pass_rate", "threshold": 1.0, "source": "quick"},
            ],
            "scores": [
                # Erlaubt die KEEP-Logic, eine Composite zu erzeugen
                {"kind": "test_count", "weight": 1.0},
            ],
        }
    )


def _fix_callable(wt: Path, prompt: str) -> None:
    """Mock-Agent-Callable: ersetzt die buggy add-Funktion durch eine korrekte."""
    target = wt / "src" / "calc.py"
    target.write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )
    return None


def test_runner_red_test_to_green_pr(red_repo: Path, tmp_path: Path) -> None:
    """Goldenes M1-Szenario: roter Test → Mock-Agent fixt → KEEP → committed."""
    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    agent = MockCodingAgent(callable_=_fix_callable)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="fix_failing_test_v1",
        initial_prompt="Fix the failing test in tests/test_calc.py.",
        focus="legacy_test_revival",
        max_iterations=2,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)

    result = runner.run()

    # Run war erfolgreich — wenigstens eine Generation hat KEEP gemacht
    assert result.decision == "pr_created", f"unexpected decision: {result.decision}"
    assert result.final_diff is not None
    assert "+    return a + b" in result.final_diff
    assert "-    return a - b" in result.final_diff
    assert result.branch is not None and result.branch.startswith("forge/")
    assert result.final_commit is not None
    assert any(g.kept for g in result.generations)

    # Events: vollständige Kette muss da sein
    events = store.events_for_run(result.run_id)
    kinds = [e.kind for e in events]
    assert EventKind.RUN_STARTED in kinds
    assert EventKind.GENERATION_STARTED in kinds
    assert EventKind.PROPOSAL_REQUESTED in kinds
    assert EventKind.PROPOSAL_RECEIVED in kinds
    assert EventKind.MUTATION_APPLIED in kinds
    assert EventKind.EVAL_STARTED in kinds
    assert EventKind.EVAL_FINISHED in kinds
    assert EventKind.DECISION_MADE in kinds
    assert EventKind.GENERATION_FINISHED in kinds
    assert EventKind.RUN_FINISHED in kinds

    # Artefakte: Prompt + Diff im Blob-Store
    refs = store.referenced_artifact_hashes()
    assert any(blobs.get_text(h) == config.initial_prompt for h in refs if blobs.has(h))

    # RunFinished trägt das richtige Outcome
    [run_finished] = [e for e in events if e.kind == EventKind.RUN_FINISHED]
    assert run_finished.payload["decision"] == "pr_created"
    assert run_finished.payload["generations_count"] >= 1

    # EvalFinished hat gates_passed=True
    eval_finished = [e for e in events if e.kind == EventKind.EVAL_FINISHED]
    assert any(e.payload["gates_passed"] is True for e in eval_finished)

    store.close()


def test_runner_discards_when_agent_breaks_test(
    red_repo: Path, tmp_path: Path
) -> None:
    """Wenn der Mock-Agent eine schlechtere Lösung macht (Test bleibt rot),
    muss der Run mit decision='no_improvement' enden, ohne Commit."""
    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    def break_more(wt: Path, prompt: str) -> None:
        # Bug bleibt — Datei nur kosmetisch ändern, Tests bleiben rot
        (wt / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a - b  # noqa\n",
            encoding="utf-8",
        )
        return None

    agent = MockCodingAgent(callable_=break_more)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="fix_failing_test_v1",
        initial_prompt="x",
        max_iterations=2,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    assert result.decision == "no_improvement"
    assert result.final_diff is None
    assert result.branch is None
    # Generationen sind alle DISCARD
    assert all(not g.kept for g in result.generations)
    # Im Store sind DECISION_MADE-Events mit kept=False
    decisions = store.events_by_kind(EventKind.DECISION_MADE)
    assert all(e.payload["kept"] is False for e in decisions)

    store.close()


def test_runner_self_terminates_on_done_signal(
    red_repo: Path, tmp_path: Path
) -> None:
    """Wenn der Agent `forge: nothing more to do` zurückgibt OHNE Änderungen,
    beendet der Run sich sofort."""
    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    # Mock-Agent: kein Diff, done-Signal — eindeutiges "fertig, nichts zu tun".
    done_result = ProposalResult(
        diff="",
        raw_response={"result": "forge: nothing more to do"},
        stop_reason="end_turn",
    )
    agent = MockCodingAgent(static_result=done_result)

    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="t1",
        initial_prompt="Try to fix the failing test.",
        max_iterations=5,  # genug Slack — sollte trotzdem nach gen 0 enden
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    assert result.decision == "self_terminated"
    assert len(result.generations) == 1, (
        f"Run hat nicht beim done-Signal abgebrochen, "
        f"{len(result.generations)} Generationen liefen"
    )
    assert result.generations[0].reason == "self_terminated"

    store.close()


def test_runner_self_termination_with_changes_still_keeps(
    red_repo: Path, tmp_path: Path
) -> None:
    """Bug-Regression: Wenn der Agent Änderungen macht UND danach
    `forge: nothing more to do` sendet, muss der Diff trotzdem committed
    werden. Self-Termination soll den Run-LOOP beenden, nicht die
    Generation prematurely skippen."""
    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    # Callable, der den Bug fixt UND done-Signal in raw_response setzt.
    def fix_and_signal_done(wt: Path, prompt: str) -> ProposalResult:
        # Fix den failing test (rot→grün)
        path = wt / "src" / "calc.py"
        path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        # Build diff manually so we can attach raw_response
        import subprocess as sp
        diff = sp.run(
            ["git", "diff"], cwd=str(wt), capture_output=True, text=True
        ).stdout
        return ProposalResult(
            diff=diff,
            raw_response={"result": "Done.\n\nforge: nothing more to do"},
            stop_reason="end_turn",
        )

    agent = MockCodingAgent(callable_=fix_and_signal_done)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="t1",
        initial_prompt="Fix the failing test.",
        max_iterations=5,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    # Erwartung: KEEP fand statt (rot→grün), Run beendet sich nach gen 0
    assert len(result.generations) == 1
    assert result.generations[0].kept is True, (
        "Bug: KEEP fand nicht statt obwohl Tests rot→grün gingen"
    )
    # decision soll pr_created sein (KEPT-Generation existiert), nicht
    # self_terminated. Self-Termination triggert Run-Loop-Abort, nicht eine
    # Decision-Override wenn KEEP-Generations da sind.
    assert result.decision == "pr_created"
    assert result.branch is not None
    assert result.final_diff is not None
    assert "+    return a + b" in result.final_diff

    store.close()


def test_runner_plan_proposed_records_invoked_agents_over_config(
    red_repo: Path, tmp_path: Path
) -> None:
    """agents_used spiegelt die vom Master GEMELDETEN Rollen, nicht das
    konfigurierte Roster.

    Der Agent meldet, dass nur architect+developer liefen (kein tester),
    obwohl config das volle Roster trägt. Das PlanProposed-Event muss die
    gemeldete Realität tragen — sonst misst forge Absicht statt Wirklichkeit.
    """
    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    def fix_with_plan(wt: Path, prompt: str) -> ProposalResult:
        (wt / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        import subprocess as sp

        diff = sp.run(
            ["git", "diff"], cwd=str(wt), capture_output=True, text=True
        ).stdout
        return ProposalResult(
            diff=diff,
            stop_reason="end_turn",
            plan_md=(
                "# Plan: fix add\n\n## Subtasks\n"
                "1. **Fix add** — file: `src/calc.py`\n\n## Risk\nlow\n"
            ),
            agents_invoked=["architect", "developer"],
        )

    agent = MockCodingAgent(callable_=fix_with_plan)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="t1",
        initial_prompt="Fix the failing test.",
        agents=["architect", "developer", "tester"],
        max_iterations=1,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    runner.run()

    plans = store.events_by_kind(EventKind.PLAN_PROPOSED)
    assert plans, "kein PlanProposed-Event emittiert"
    # Gemeldete Realität (ohne tester) gewinnt über das volle config-Roster.
    assert plans[0].payload["agents_used"] == ["architect", "developer"]

    store.close()


def test_runner_emits_project_memory_artifact_when_seed_present(
    red_repo: Path, tmp_path: Path
) -> None:
    """ProposalRequested records project_memory when .forge/memory.md exists."""
    forge_dir = red_repo / ".forge"
    forge_dir.mkdir()
    seed = "Stable layout: src/ for code, tests/ for xUnit."
    (forge_dir / "memory.md").write_text(seed, encoding="utf-8")

    spec = _spec_for_red_repo()
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    agent = MockCodingAgent(callable_=_fix_callable)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="fix_failing_test_v1",
        initial_prompt="Fix the failing test in tests/test_calc.py.",
        max_iterations=1,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()
    assert result.decision == "pr_created"

    requested = [
        e
        for e in store.events_for_run(result.run_id)
        if e.kind == EventKind.PROPOSAL_REQUESTED
    ]
    assert requested
    evt = requested[0]
    assert evt.payload["context_artifact_keys"] == ["prompt", "project_memory"]
    assert "project_memory" in evt.artifacts
    memory_text = blobs.get_text(evt.artifacts["project_memory"])
    assert seed in memory_text

    prompt_text = blobs.get_text(evt.artifacts["prompt"])
    assert "## forge project memory" in prompt_text
    assert seed in prompt_text

    store.close()


def test_runner_blocks_capability_violation(
    red_repo: Path, tmp_path: Path
) -> None:
    """Agent versucht in einen Forbidden-Path zu schreiben → Run abgebrochen."""
    spec_dict = {
        "spec_version": "1.0",
        "name": "red-repo",
        "cost_caps": {
            "per_generation_usd": "0.50",
            "per_run_usd": "5.00",
            "per_project_per_day_usd": "30.00",
            "per_project_per_month_usd": "500.00",
        },
        "surfaces": {"code": {"paths": ["src/"], "type": "code"}},
        "forbidden": ["tests/**"],
        "capabilities": {"run": ["pytest *"]},
        "eval_suites": {
            "quick": {
                "cmd": f'"{sys.executable}" -m pytest -q',
                "budget_s": 60,
                "parses": "pytest_json",
            }
        },
    }
    spec = ProjectSpec.model_validate(spec_dict)

    def edit_forbidden(wt: Path, prompt: str) -> None:
        # Editiert eine forbidden-Datei
        (wt / "tests" / "test_calc.py").write_text(
            "def test_add():\n    assert True  # disabled by hostile agent\n",
            encoding="utf-8",
        )
        return None

    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    agent = MockCodingAgent(callable_=edit_forbidden)
    config = RunConfig(
        spec=spec,
        project="red-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=red_repo,
        prompt_template_id="t1",
        initial_prompt="x",
        max_iterations=2,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)
    result = runner.run()

    assert result.decision == "guardrail_blocked"
    # GuardrailViolation-Event muss im Store sein
    violations = store.events_by_kind(EventKind.GUARDRAIL_VIOLATION)
    assert len(violations) >= 1
    assert violations[0].payload["guardrail_id"] == "forbidden_path"

    store.close()
