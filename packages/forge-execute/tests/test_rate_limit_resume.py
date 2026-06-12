"""Inkrement 1: Erkennen & sauber melden eines Claude-Usage-/Session-Limits.

Belegt das beobachtete Verhalten aus einem echten WP7-Run: claude meldet das
Limit als ``result``-Event mit ``subtype: "success"`` ABER ``is_error: true`` +
``api_error_status: 429`` und Text ``"You've hit your session limit · resets
1pm (Europe/Vienna)"``. forge muss daraus einen fortsetzbaren ``rate_limited``-
Run machen (echter Cost, Resume-Anker, Worktree erhalten), statt ihn als
``no_improvement`` mit $0 zu verbuchen.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.blobs import BlobStore
from forge_core.events import EventKind
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from forge_execute.agents import MockCodingAgent
from forge_execute.agents.base import CodingAgentRateLimited
from forge_execute.agents.claude_cli import (
    _extract_session_id,
    _is_rate_limited,
    _parse_reset_time,
)
from forge_execute.runner import RunConfig, SequentialRunner

# Das echte result-Event eines session-limit-Abbruchs (gekürzt auf die Felder,
# die forge auswertet) — Shape verifiziert gegen einen produktiven WP7-Run.
_REAL_LIMIT_RESULT = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "api_error_status": 429,
    "num_turns": 45,
    "result": "You've hit your session limit · resets 1pm (Europe/Vienna)",
    "stop_reason": "stop_sequence",
    "session_id": "dd824938-e08f-4d4a-9d20-ebe65fdd5b36",
    "total_cost_usd": 9.911621150000002,
}


# --- Helper-Unit-Tests ---------------------------------------------------


def test_is_rate_limited_detects_429_despite_success_subtype() -> None:
    # Der Knackpunkt: subtype ist "success", trotzdem ein Limit.
    assert _is_rate_limited(_REAL_LIMIT_RESULT) is True


def test_is_rate_limited_false_on_normal_success() -> None:
    assert _is_rate_limited({"type": "result", "subtype": "success", "is_error": False}) is False


def test_is_rate_limited_text_fallback_without_status() -> None:
    # Ältere CLI ohne api_error_status: Text-Fallback greift.
    assert _is_rate_limited(
        {"is_error": True, "result": "You've hit your usage limit · resets 9am (UTC)"}
    ) is True


def test_extract_session_id_from_stream_lines() -> None:
    lines = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "abc-123"}),
        json.dumps({"type": "assistant", "session_id": "abc-123"}),
    ]
    assert _extract_session_id(lines) == "abc-123"


def test_parse_reset_time_pm_with_timezone() -> None:
    now = datetime(2026, 6, 12, 9, 0, tzinfo=UTC)  # 11:00 Europe/Vienna (DST)
    reset = _parse_reset_time(_REAL_LIMIT_RESULT["result"], now=now)
    # 1pm Europe/Vienna am 12.6.2026 = 11:00 UTC (Sommerzeit, UTC+2).
    assert reset == datetime(2026, 6, 12, 11, 0, tzinfo=UTC)


def test_parse_reset_time_rolls_to_next_day_when_passed() -> None:
    now = datetime(2026, 6, 12, 12, 0, tzinfo=UTC)  # nach 1pm Vienna
    reset = _parse_reset_time("resets 1pm (Europe/Vienna)", now=now)
    assert reset is not None and reset.date() == datetime(2026, 6, 13).date()


def test_parse_reset_time_unparseable_returns_none() -> None:
    assert _parse_reset_time("limit reached, try later") is None


# --- Runner-Integrationstest --------------------------------------------


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def green_repo(tmp_path: Path) -> Path:
    """Mini-Repo, dessen Test bereits grün ist (Baseline soll sauber laufen)."""
    repo = tmp_path / "repo"
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
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.pyc\n.pytest_cache/\n.coverage\n", encoding="utf-8"
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "initial (green)")
    return repo


def _spec(tmp_path: Path) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "limit-repo",
            "cost_caps": {
                "per_generation_usd": "50.00",
                "per_run_usd": "500.00",
                "per_project_per_day_usd": "3000.00",
                "per_project_per_month_usd": "5000.00",
            },
            "surfaces": {"code": {"paths": ["src/"], "type": "code"}},
            "forbidden": [],
            "capabilities": {"run": ["pytest *", "python *"]},
            "eval_suites": {
                "quick": {
                    "cmd": f'"{sys.executable}" -m pytest -q --no-header --tb=no',
                    "budget_s": 60,
                    "parses": "pytest_json",
                },
            },
            "gates": [{"kind": "pytest_pass_rate", "threshold": 1.0, "source": "quick"}],
            "scores": [{"kind": "test_count", "weight": 1.0}],
        }
    )


def test_runner_rate_limited_is_resumable_not_lost(
    green_repo: Path, tmp_path: Path
) -> None:
    """Session-Limit mitten im Run → decision=rate_limited, echter Cost gebucht,
    Partial-Arbeit als WIP committet, RunResumeScheduled mit Anker, Branch erhalten."""
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")

    reset_at = datetime(2026, 6, 12, 11, 0, tzinfo=UTC)

    def limit_callable(wt: Path, prompt: str) -> None:
        # Partial-Arbeit, BEVOR das Limit zuschlägt — darf nicht verloren gehen.
        (wt / "src" / "partial.py").write_text(
            "# work in progress before the limit\n", encoding="utf-8"
        )
        raise CodingAgentRateLimited(
            "claude hit a usage/session limit",
            session_id="dd824938-e08f-4d4a-9d20-ebe65fdd5b36",
            reset_at=reset_at,
            cost_usd=Decimal("9.91"),
            turns_used=45,
        )

    agent = MockCodingAgent(callable_=limit_callable)
    config = RunConfig(
        spec=_spec(tmp_path),
        project="limit-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=green_repo,
        prompt_template_id="impl_v1",
        initial_prompt="Implement WP7.",
        issue_number=7,
        max_iterations=2,
    )
    runner = SequentialRunner(config=config, agent=agent, store=store, blobs=blobs)

    result = runner.run()

    # 1. Fortsetzbar, nicht gescheitert.
    assert result.decision == "rate_limited"
    # 2. Echter Cost gebucht (der ursprüngliche Bug: $0).
    assert result.total_cost_usd == Decimal("9.91")
    # 3. Branch + WIP-Commit erhalten (Resume baut darauf auf).
    assert result.branch is not None and result.branch.startswith("forge/")
    assert result.final_commit is not None

    events = store.events_for_run(result.run_id)
    kinds = [e.kind for e in events]

    # 4. RunFinished trägt rate_limited + den echten Cost.
    [run_finished] = [e for e in events if e.kind == EventKind.RUN_FINISHED]
    assert run_finished.payload["decision"] == "rate_limited"
    assert Decimal(str(run_finished.payload["total_cost_usd"])) == Decimal("9.91")

    # 5. ProposalReceived: session_id + stop_reason, als Fehlschlag markiert.
    [proposal] = [e for e in events if e.kind == EventKind.PROPOSAL_RECEIVED]
    assert proposal.payload["stop_reason"] == "rate_limited"
    assert proposal.payload["session_id"] == "dd824938-e08f-4d4a-9d20-ebe65fdd5b36"
    assert proposal.success is False

    # 6. RunResumeScheduled trägt den vollständigen Anker.
    assert EventKind.RUN_RESUME_SCHEDULED in kinds
    [resume] = [e for e in events if e.kind == EventKind.RUN_RESUME_SCHEDULED]
    assert resume.payload["original_run_id"] == result.run_id
    assert resume.payload["resume_session_id"] == "dd824938-e08f-4d4a-9d20-ebe65fdd5b36"
    assert resume.payload["resume_at"] is not None
    assert resume.payload["wip_commit"] == result.final_commit
    assert resume.payload["issue_number"] == 7

    # 7. Partial-Arbeit überlebt: im WIP-Worktree vorhanden UND committet
    #    (kein revert / git clean -fdx auf diesem Pfad).
    worktree_path = Path(resume.payload["resume_worktree"])
    assert (worktree_path / "src" / "partial.py").exists()
    committed = subprocess.run(
        ["git", "show", f"{result.final_commit}:src/partial.py"],
        cwd=str(worktree_path),
        capture_output=True,
        text=True,
    )
    assert committed.returncode == 0
    assert "work in progress" in committed.stdout

    store.close()


def test_resume_attaches_to_worktree_and_continues(
    green_repo: Path, tmp_path: Path
) -> None:
    """Run A läuft ins Limit; Run B setzt unter DERSELBEN run_id + session_id
    fort, dockt an den eingefrorenen Worktree an (sieht die Partial-Arbeit) und
    schließt den Task ab → pr_created."""
    store = EventStore(tmp_path / "events.duckdb")
    blobs = BlobStore(tmp_path / "blobs")
    spec = _spec(tmp_path)
    session_id = "dd824938-e08f-4d4a-9d20-ebe65fdd5b36"

    def limit_callable(wt: Path, prompt: str) -> None:
        (wt / "src" / "partial.py").write_text("# wip\n", encoding="utf-8")
        raise CodingAgentRateLimited(
            "limit",
            session_id=session_id,
            reset_at=None,
            cost_usd=Decimal("3.00"),
            turns_used=10,
        )

    config_a = RunConfig(
        spec=spec,
        project="limit-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=green_repo,
        prompt_template_id="impl_v1",
        initial_prompt="Implement WP7.",
        issue_number=7,
        max_iterations=1,
    )
    runner_a = SequentialRunner(
        config=config_a,
        agent=MockCodingAgent(callable_=limit_callable),
        store=store,
        blobs=blobs,
    )
    result_a = runner_a.run()
    assert result_a.decision == "rate_limited"

    # --- Run B: Resume ---------------------------------------------------
    def complete_callable(wt: Path, prompt: str) -> None:
        # Beweist Attach an Run As Worktree: die Partial-Datei ist da.
        assert (wt / "src" / "partial.py").exists()
        # Neuen grünen Test ergänzen → test_count steigt → KEEP → pr_created.
        (wt / "tests" / "test_more.py").write_text(
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path.insert(0, str(Path(__file__).parent.parent))\n"
            "from src.calc import add\n\n"
            "def test_more():\n    assert add(1, 1) == 2\n",
            encoding="utf-8",
        )
        return None

    agent_b = MockCodingAgent(callable_=complete_callable)
    config_b = RunConfig(
        spec=spec,
        project="limit-repo",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=green_repo,
        prompt_template_id="impl_v1",
        initial_prompt="continue",
        issue_number=7,
        max_iterations=1,
    )
    runner_b = SequentialRunner(
        config=config_b,
        agent=agent_b,
        store=store,
        blobs=blobs,
        run_id=result_a.run_id,
        resume_session_id=session_id,
    )
    result_b = runner_b.run()

    # Gleiche Run-Linie + session_id durchgereicht. Dass die in-callable-
    # Assertion (partial.py existiert) nicht flog, beweist: Run B lief im
    # eingefrorenen Worktree von Run A (attach), nicht in einem frischen.
    assert result_b.run_id == result_a.run_id
    assert agent_b.resume_session_ids[0] == session_id
    assert len(agent_b.calls) == 1  # Resume hat den Agent genau einmal gerufen

    # Beide Runs teilen die Event-Linie (gleiche run_id). Run A: PROPOSAL_RECEIVED
    # success=False (rate_limited). Run B: success=True (Resume-Propose lief durch).
    proposals = [
        e
        for e in store.events_for_run(result_a.run_id)
        if e.kind == EventKind.PROPOSAL_RECEIVED
    ]
    assert any(p.success is False for p in proposals)  # Run A (Limit)
    assert any(p.success is True for p in proposals)  # Run B (Resume erfolgreich)

    store.close()
