"""Tests für forge_execute.agents."""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from forge_execute.agents import (
    ClaudeCodeCLIAgent,
    CodingAgent,
    CodingAgentError,
    MockCodingAgent,
    ProposalResult,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@forge.local")
    _git(repo_root, "config", "user.name", "forge-test")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "initial")
    return repo_root


# --- MockCodingAgent ----------------------------------------------------


def test_mock_static_result_used_for_every_call() -> None:
    expected = ProposalResult(diff="--- a\n+++ b\n", tokens_in=42, tokens_out=10)
    agent = MockCodingAgent(static_result=expected)
    r1 = agent.propose(worktree=Path("/tmp"), prompt="x", max_turns=1, budget_usd=Decimal("1"))
    r2 = agent.propose(worktree=Path("/tmp"), prompt="y", max_turns=1, budget_usd=Decimal("1"))
    assert r1 is expected
    assert r2 is expected
    assert len(agent.calls) == 2


def test_mock_sequence_consumes_one_per_call() -> None:
    agent = MockCodingAgent(
        sequence=[
            ProposalResult(diff="first"),
            ProposalResult(diff="second"),
        ]
    )
    a = agent.propose(worktree=Path("/tmp"), prompt="x", max_turns=1, budget_usd=Decimal("1"))
    b = agent.propose(worktree=Path("/tmp"), prompt="y", max_turns=1, budget_usd=Decimal("1"))
    assert a.diff == "first"
    assert b.diff == "second"
    with pytest.raises(IndexError):
        agent.propose(worktree=Path("/tmp"), prompt="z", max_turns=1, budget_usd=Decimal("1"))


def test_mock_callable_modifies_worktree(repo: Path) -> None:
    """Callable-Modus: Funktion modifiziert Worktree, Mock baut Diff aus git."""

    def fix(wt: Path, prompt: str) -> None:  # gibt nichts zurück
        path = wt / "src" / "calc.py"
        path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return None

    agent = MockCodingAgent(callable_=fix)
    result = agent.propose(
        worktree=repo,
        prompt="fix add",
        max_turns=1,
        budget_usd=Decimal("1"),
    )
    assert "+    return a + b" in result.diff
    assert "-    return a - b" in result.diff
    assert result.has_changes is True
    assert result.stop_reason == "end_turn"


def test_mock_callable_can_return_explicit_result(repo: Path) -> None:
    explicit = ProposalResult(
        diff="custom-diff",
        tokens_in=100,
        tokens_out=50,
        cost_usd=Decimal("0.01"),
    )

    def fn(wt: Path, prompt: str) -> ProposalResult:
        return explicit

    agent = MockCodingAgent(callable_=fn)
    out = agent.propose(
        worktree=repo, prompt="x", max_turns=1, budget_usd=Decimal("1")
    )
    assert out is explicit


def test_mock_rejects_multiple_modes() -> None:
    with pytest.raises(ValueError):
        MockCodingAgent(
            static_result=ProposalResult(diff=""),
            sequence=[ProposalResult(diff="")],
        )


def test_mock_satisfies_protocol() -> None:
    agent: CodingAgent = MockCodingAgent(static_result=ProposalResult(diff=""))
    assert agent is not None  # type-only assertion via assignment


# --- ClaudeCodeCLIAgent -------------------------------------------------


def test_claude_cli_raises_on_missing_binary(repo: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    agent = ClaudeCodeCLIAgent(claude_bin="this-binary-does-not-exist-99999")
    with pytest.raises(CodingAgentError, match="not found"):
        agent.propose(
            worktree=repo,
            prompt="x",
            max_turns=1,
            budget_usd=Decimal("1"),
        )


def test_claude_cli_satisfies_protocol() -> None:
    agent: CodingAgent = ClaudeCodeCLIAgent()
    assert agent is not None


def test_multi_agent_default_timeout_is_one_hour() -> None:
    """Greenfield-Milestones mit vollem Roster brauchen mehr als 25 Min —
    sonst kappt der Wallclock-Timeout Eval/Judge/Reviewer (Regression)."""
    from forge_execute.agents.claude_cli import (
        DEFAULT_TIMEOUT_S,
        MULTI_AGENT_TIMEOUT_S,
    )

    multi = ClaudeCodeCLIAgent(agents=["architect", "developer", "tester"])
    assert multi.timeout_s == MULTI_AGENT_TIMEOUT_S == 3600

    single = ClaudeCodeCLIAgent(agents=["developer"])
    assert single.timeout_s == DEFAULT_TIMEOUT_S == 600


def test_explicit_timeout_overrides_default() -> None:
    """`--agent-timeout` (→ timeout_s) schlägt den Default in beiden Modi."""
    multi = ClaudeCodeCLIAgent(agents=["architect", "developer"], timeout_s=7200)
    assert multi.timeout_s == 7200

    single = ClaudeCodeCLIAgent(agents=["developer"], timeout_s=120)
    assert single.timeout_s == 120


def test_zero_or_none_timeout_falls_back_to_default() -> None:
    """0/None (kein --agent-timeout gesetzt) ⇒ moduspezifischer Default."""
    from forge_execute.agents.claude_cli import MULTI_AGENT_TIMEOUT_S

    assert ClaudeCodeCLIAgent(agents=["developer", "tester"], timeout_s=None).timeout_s == (
        MULTI_AGENT_TIMEOUT_S
    )
    assert ClaudeCodeCLIAgent(agents=["developer", "tester"], timeout_s=0).timeout_s == (
        MULTI_AGENT_TIMEOUT_S
    )


# --- stream-json: Pump, Log, Result-Extraktion ---------------------------


def test_pump_streaming_writes_envelopes_and_collects_lines(tmp_path: Path) -> None:
    """Jede stdout-Zeile landet sofort als {"ts","event"}-Envelope im Log."""
    import json
    import subprocess
    import sys

    from forge_execute.agents.claude_cli import _pump_streaming

    script = (
        "import json\n"
        "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm'}))\n"
        "print(json.dumps({'type': 'result', 'subtype': 'success', 'num_turns': 2}))\n"
    )
    log_path = tmp_path / "stream.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    lines, stderr, timed_out = _pump_streaming(proc, timeout_s=30, log_path=log_path)

    assert timed_out is False
    assert stderr == ""
    assert len(lines) == 2

    envelopes = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert len(envelopes) == 2
    assert all("ts" in e and "event" in e for e in envelopes)
    assert envelopes[0]["event"]["subtype"] == "init"
    assert envelopes[1]["event"]["type"] == "result"


def test_pump_streaming_kills_on_timeout(tmp_path: Path) -> None:
    """Deadline überschritten ⇒ Prozessbaum gekillt, timed_out=True, Log bleibt."""
    import json
    import subprocess
    import sys
    import time as time_mod

    from forge_execute.agents.claude_cli import _pump_streaming

    script = (
        "import json, time, sys\n"
        "print(json.dumps({'type': 'assistant'}), flush=True)\n"
        "time.sleep(60)\n"
    )
    log_path = tmp_path / "stream.jsonl"
    proc = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    start = time_mod.monotonic()
    lines, _stderr, timed_out = _pump_streaming(proc, timeout_s=2, log_path=log_path)
    elapsed = time_mod.monotonic() - start

    assert timed_out is True
    assert elapsed < 30, "Kill muss deutlich vor den 60s des Kind-Prozesses greifen"
    # Die vor dem Timeout emittierte Zeile ist gesichert — Diagnose bleibt möglich.
    assert len(lines) == 1
    assert json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])["event"][
        "type"
    ] == "assistant"


def test_extract_result_event_finds_last_result() -> None:
    import json

    from forge_execute.agents.claude_cli import _extract_result_event

    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {}}),
        json.dumps({"type": "result", "subtype": "success", "num_turns": 7}),
    ]
    raw = _extract_result_event(lines)
    assert raw["num_turns"] == 7

    assert _extract_result_event(lines[:2]) == {}
    assert _extract_result_event(["kein json"]) == {}


def test_stream_log_path_lands_in_forge_logs_for_run_worktrees(tmp_path: Path) -> None:
    """Standard-Layout <repo>/.forge/worktrees/<run_id> ⇒ Log in .forge/logs/<run_id>/
    (außerhalb des Worktrees — git clean -fdx bei DISCARD darf es nicht löschen)."""
    from forge_execute.agents.claude_cli import _new_stream_log_path

    wt = tmp_path / ".forge" / "worktrees" / "01RUNID"
    wt.mkdir(parents=True)
    log = _new_stream_log_path(wt, label="propose")
    assert log.parent == tmp_path / ".forge" / "logs" / "01RUNID"
    assert log.name.startswith("propose-")
    assert log.suffix == ".jsonl"

    # Fallback-Layout: beliebiges Verzeichnis ⇒ .claude/forge-logs/ im Worktree.
    other = tmp_path / "elsewhere"
    other.mkdir()
    fallback = _new_stream_log_path(other, label="propose")
    assert fallback.parent == other / ".claude" / "forge-logs"


def test_stage_untracked_makes_new_files_visible_in_diff(repo: Path) -> None:
    """Der Greenfield-Bug: neu angelegte Files fehlen ohne intent-to-add im
    `git diff` und damit im Proposal-Diff. `_stage_untracked_for_diff` macht
    sie sichtbar, `.claude/` bleibt ausgeschlossen."""
    from forge_execute.agents.claude_cli import _git, _stage_untracked_for_diff

    base_sha = _git(repo, "rev-parse", "HEAD")
    # Agent legt eine neue Datei an (untracked) und ein transientes Subagent-md.
    (repo / "src" / "new_module.py").write_text(
        "def f():\n    return 1\n", encoding="utf-8"
    )
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "architect.md").write_text("x", encoding="utf-8")

    # Vor dem Fix: die neue Datei fehlt im Diff gegen base.
    assert "new_module.py" not in _git(repo, "diff", base_sha)

    _stage_untracked_for_diff(repo)
    diff = _git(repo, "diff", base_sha)
    assert "src/new_module.py" in diff
    assert "+    return 1" in diff
    # Transiente Subagent-Files dürfen nie im Diff landen.
    assert ".claude/" not in diff
    assert "architect.md" not in diff


# --- Subagent installation -------------------------------------------------


def test_subagent_templates_are_packaged() -> None:
    """architect.md / developer.md / tester.md müssen via importlib.resources
    auffindbar sein, sonst landet `forge run --multi-agent` ohne Subagents."""
    from forge_execute.agents.templates import list_templates, templates_dir

    assert templates_dir().is_dir()
    names = {p.name for p in list_templates()}
    assert names >= {"architect.md", "developer.md", "tester.md"}


def test_install_subagents_copies_templates_into_worktree(repo: Path) -> None:
    """`_install_subagents` legt die Markdowns unter <wt>/.claude/agents/ ab."""
    from forge_execute.agents.claude_cli import _install_subagents

    _install_subagents(repo)
    target = repo / ".claude" / "agents"
    assert target.is_dir()
    assert (target / "architect.md").is_file()
    assert (target / "developer.md").is_file()
    assert (target / "tester.md").is_file()
    # Inhalt: erwartet eine YAML-Frontmatter-Zeile mit "name:"
    text = (target / "architect.md").read_text(encoding="utf-8")
    assert "name: architect" in text


def test_install_subagents_project_override_wins(repo: Path) -> None:
    """Spec v0.3 Teil 6.5 Designentscheidung 5.1: per-Projekt-Override
    in `<repo>/.forge/agents/<name>.md` überschreibt forge-Defaults."""
    from forge_execute.agents.claude_cli import _install_subagents

    # Projekt-Override für architect anlegen
    override_dir = repo / ".forge" / "agents"
    override_dir.mkdir(parents=True)
    (override_dir / "architect.md").write_text(
        "---\nname: architect\n---\n# CUSTOM ARCHITECT — pinta-spezifisch\n",
        encoding="utf-8",
    )

    _install_subagents(repo)
    target = repo / ".claude" / "agents"

    # Override hat sich durchgesetzt
    architect_text = (target / "architect.md").read_text(encoding="utf-8")
    assert "CUSTOM ARCHITECT" in architect_text
    # Andere Defaults sind trotzdem da
    assert (target / "developer.md").is_file()
    assert (target / "tester.md").is_file()


def test_install_subagents_finds_override_above_worktree(
    tmp_path: Path,
) -> None:
    """Bei einem Worktree unter <repo>/.forge/worktrees/<id>/ findet
    der Lookup das `.forge/agents/`-Verzeichnis im Repo-Root."""
    from forge_execute.agents.claude_cli import _install_subagents

    repo_root = tmp_path / "repo"
    worktree = repo_root / ".forge" / "worktrees" / "01ABC"
    worktree.mkdir(parents=True)

    override_dir = repo_root / ".forge" / "agents"
    override_dir.mkdir(parents=True)
    (override_dir / "developer.md").write_text(
        "---\nname: developer\n---\n# Project-specific developer\n",
        encoding="utf-8",
    )

    _install_subagents(worktree)
    text = (worktree / ".claude" / "agents" / "developer.md").read_text(
        encoding="utf-8"
    )
    assert "Project-specific developer" in text


def test_install_subagents_roster_filters_templates(repo: Path) -> None:
    """Roster begrenzt, welche Subagents installiert werden."""
    from forge_execute.agents.claude_cli import _install_subagents

    _install_subagents(repo, agents=["architect", "developer"])
    target = repo / ".claude" / "agents"
    assert (target / "architect.md").is_file()
    assert (target / "developer.md").is_file()
    # tester ist NICHT im Roster → nicht installiert.
    assert not (target / "tester.md").exists()


def test_install_subagents_roster_filters_project_override(repo: Path) -> None:
    """Auch Projekt-Overlays außerhalb des Rosters werden gefiltert."""
    from forge_execute.agents.claude_cli import _install_subagents

    override_dir = repo / ".forge" / "agents"
    override_dir.mkdir(parents=True)
    (override_dir / "tester.md").write_text(
        "---\nname: tester\n---\n# CUSTOM TESTER\n", encoding="utf-8"
    )

    _install_subagents(repo, agents=["developer"])
    target = repo / ".claude" / "agents"
    assert (target / "developer.md").is_file()
    assert not (target / "tester.md").exists()


def test_augment_tools_adds_task_when_missing() -> None:
    from forge_execute.agents.claude_cli import _augment_tools_for_multi_agent

    assert _augment_tools_for_multi_agent(None) == "Task"
    assert _augment_tools_for_multi_agent("") == "Task"
    assert _augment_tools_for_multi_agent("Read,Edit") == "Read,Edit,Task"
    # idempotent
    assert _augment_tools_for_multi_agent("Read,Task,Edit") == "Read,Task,Edit"


# --- Roster resolution & orchestrator prompt ------------------------------


def test_normalize_agents_defaults_and_ordering() -> None:
    from forge_execute.agents.templates import DEFAULT_AGENTS, normalize_agents

    assert normalize_agents(None) == list(DEFAULT_AGENTS)
    assert normalize_agents([]) == list(DEFAULT_AGENTS)
    # Reihenfolge folgt der Pipeline, Duplikate/Unbekanntes fallen raus.
    assert normalize_agents(["tester", "architect", "bogus", "tester"]) == [
        "architect",
        "tester",
    ]


def test_normalize_agents_orders_reviewer_last() -> None:
    from forge_execute.agents.templates import normalize_agents

    # reviewer ist eine bekannte Rolle und folgt in der Pipeline-Ordnung
    # hinter tester.
    assert normalize_agents(["reviewer", "developer", "architect"]) == [
        "architect",
        "developer",
        "reviewer",
    ]


def test_roster_needs_orchestration() -> None:
    from forge_execute.agents.templates import roster_needs_orchestration

    assert roster_needs_orchestration(["developer"]) is False
    assert roster_needs_orchestration(["architect", "developer"]) is True
    assert roster_needs_orchestration(["developer", "tester"]) is True
    assert roster_needs_orchestration(["developer", "reviewer"]) is True


def test_build_orchestrator_prompt_full_roster_has_plan_markers() -> None:
    from forge_execute.agents.templates import (
        PLAN_BEGIN_MARKER,
        build_orchestrator_prompt,
    )

    prompt = build_orchestrator_prompt(["architect", "developer", "tester"])
    assert "architect" in prompt
    assert "tester" in prompt
    assert PLAN_BEGIN_MARKER in prompt


def test_build_orchestrator_prompt_without_architect_omits_plan() -> None:
    from forge_execute.agents.templates import (
        PLAN_BEGIN_MARKER,
        build_orchestrator_prompt,
    )

    prompt = build_orchestrator_prompt(["developer", "tester"])
    # Kein architect → kein Plan-Schritt, keine Marker.
    assert PLAN_BEGIN_MARKER not in prompt
    assert "tester" in prompt


def test_build_orchestrator_prompt_without_tester_omits_verification() -> None:
    from forge_execute.agents.templates import build_orchestrator_prompt

    prompt = build_orchestrator_prompt(["architect", "developer"])
    assert "architect" in prompt
    assert "verification suite" not in prompt


def test_build_orchestrator_prompt_with_reviewer_weaves_review_step() -> None:
    from forge_execute.agents.templates import build_orchestrator_prompt

    prompt = build_orchestrator_prompt(
        ["architect", "developer", "tester", "reviewer"]
    )
    assert "reviewer" in prompt
    # Der Reviewer-Schritt nennt die BLOCKING-Findings-Schleife und das
    # Finalize-Verbot.
    assert "BLOCKING" in prompt


def test_build_orchestrator_prompt_without_reviewer_omits_review() -> None:
    from forge_execute.agents.templates import build_orchestrator_prompt

    prompt = build_orchestrator_prompt(["architect", "developer", "tester"])
    assert "reviewer" not in prompt
    assert "BLOCKING" not in prompt


def test_unknown_agents_flags_dropped_roles() -> None:
    from forge_execute.agents.templates import unknown_agents

    assert unknown_agents(None) == []
    assert unknown_agents(["architect", "developer", "tester", "reviewer"]) == []
    # Tippfehler und spec-reservierte-aber-nicht-implementierte Rollen werden
    # sichtbar (Input-Reihenfolge), bekannte fallen raus.
    assert unknown_agents(["architect", "operations", "typo"]) == [
        "operations",
        "typo",
    ]


def test_build_orchestrator_prompt_requests_agents_marker() -> None:
    from forge_execute.agents.templates import (
        AGENTS_BEGIN_MARKER,
        build_orchestrator_prompt,
    )

    # Beide Roster-Varianten (mit/ohne architect) fordern den Agents-Block an.
    assert AGENTS_BEGIN_MARKER in build_orchestrator_prompt(
        ["architect", "developer", "tester"]
    )
    assert AGENTS_BEGIN_MARKER in build_orchestrator_prompt(["developer", "tester"])


def test_extract_agents_from_master_output_roundtrip() -> None:
    from forge_execute.agents.templates import (
        AGENTS_BEGIN_MARKER,
        AGENTS_END_MARKER,
        extract_agents_from_master_output,
    )

    blob = (
        f"summary text\n{AGENTS_BEGIN_MARKER}\n"
        "tester, developer, bogus, tester\n"
        f"{AGENTS_END_MARKER}\ntrailing"
    )
    # Bekannte Rollen in Pipeline-Ordnung, Garbage + Duplikate gefiltert.
    assert extract_agents_from_master_output(blob) == ["developer", "tester"]


def test_extract_agents_from_master_output_absent_returns_none() -> None:
    from forge_execute.agents.templates import extract_agents_from_master_output

    assert extract_agents_from_master_output("no markers anywhere") is None
    # Marker da, aber nur Unbekanntes drin → None (Caller fällt auf Config).
    from forge_execute.agents.templates import (
        AGENTS_BEGIN_MARKER,
        AGENTS_END_MARKER,
    )

    empty = f"{AGENTS_BEGIN_MARKER}\nbogus only\n{AGENTS_END_MARKER}"
    assert extract_agents_from_master_output(empty) is None


def test_claude_agent_roster_derives_multi_agent() -> None:
    """`agents`-Roster ist die Quelle der Wahrheit für multi_agent."""
    lone = ClaudeCodeCLIAgent(agents=["developer"])
    assert lone.agents == ["developer"]
    assert lone.multi_agent is False

    team = ClaudeCodeCLIAgent(agents=["tester", "architect"])
    assert team.agents == ["architect", "tester"]  # normalisiert
    assert team.multi_agent is True


def test_claude_agent_multi_agent_flag_backcompat() -> None:
    """Alt-Pfad: multi_agent=True → volles Default-Roster."""
    legacy = ClaudeCodeCLIAgent(multi_agent=True)
    assert legacy.agents == ["architect", "developer", "tester"]
    assert legacy.multi_agent is True

    single = ClaudeCodeCLIAgent(multi_agent=False)
    assert single.agents == ["developer"]
    assert single.multi_agent is False


# --- Plan extraction from master output -----------------------------------


def test_extract_plan_from_master_output_with_markers() -> None:
    from forge_execute.agents.templates import extract_plan_from_master_output

    text = """Some intro

---FORGE-PLAN-BEGIN---
# Plan: do X

## Subtasks
1. one
2. two

## Risk
low
---FORGE-PLAN-END---

## Run summary
- subtasks done
"""
    plan = extract_plan_from_master_output(text)
    assert plan is not None
    assert plan.startswith("# Plan: do X")
    assert "Subtasks" in plan
    assert "Run summary" not in plan


def test_extract_plan_returns_none_without_markers() -> None:
    from forge_execute.agents.templates import extract_plan_from_master_output

    text = "Just a normal message, no plan markers here."
    assert extract_plan_from_master_output(text) is None


def test_extract_plan_returns_none_with_only_begin_marker() -> None:
    from forge_execute.agents.templates import extract_plan_from_master_output

    text = "---FORGE-PLAN-BEGIN---\nIncomplete plan, no end marker."
    assert extract_plan_from_master_output(text) is None


def test_extract_plan_returns_none_when_block_empty() -> None:
    from forge_execute.agents.templates import extract_plan_from_master_output

    text = "before\n---FORGE-PLAN-BEGIN---\n\n---FORGE-PLAN-END---\nafter"
    assert extract_plan_from_master_output(text) is None
