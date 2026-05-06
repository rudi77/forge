"""Claude Code CLI als Coding-Agent.

Implementiert `CodingAgent`-Protocol via Subprozess-Aufruf von `claude -p`
(headless mode). Begründung in todos.txt: das CLI hat Tool-Use-Loop,
File-Editing, Patch-Application produktiv-fertig. Wenn man das selbst
implementiert, sind das ~2000 Zeilen Code für eine Funktionalität, die
schon existiert.

Sicherheit: `--allowedTools` ist die wichtigste Verteidigungslinie — es
wirkt im Subprozess, bevor der Agent etwas tun könnte. forges
`Capabilities.allowed_tools_string()` liefert den exakten String.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_execute._venv import venv_aware_env
from forge_execute.agents.base import (
    CodingAgent,
    CodingAgentError,
    CodingAgentTimeout,
    ProposalResult,
)
from forge_execute.agents.templates import (
    ORCHESTRATOR_SYSTEM_PROMPT,
    list_templates,
)
from forge_execute.evaluators.command import _kill_tree

_IS_WINDOWS = platform.system() == "Windows"


# Default-Wallclock-Limit: ein Vielfaches der LLM-Latenz, falls budget_usd
# nicht limitiert. Cost-Caps machen das eigentliche Limit aus.
DEFAULT_TIMEOUT_S = 600


class ClaudeCodeCLIAgent:
    """Spricht Claude Code CLI im Headless-Modus an.

    Verwendung::

        agent = ClaudeCodeCLIAgent(claude_bin="claude", default_model="sonnet")
        result = agent.propose(
            worktree=wt.path,
            prompt="Fixe den failing test in tests/test_quote_calculator.py",
            max_turns=8,
            budget_usd=Decimal("0.50"),
            allowed_tools="Read,Edit,Write,Bash(pytest:*),Bash(ruff:*)",
        )
    """

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        default_model: str | None = None,
        permission_mode: str = "bypassPermissions",
        timeout_s: int | None = None,
        multi_agent: bool = False,
    ) -> None:
        self.claude_bin = claude_bin
        self.default_model = default_model
        self.permission_mode = permission_mode
        self.multi_agent = multi_agent
        # Multi-Agent-Runs spawnen Subagents (architect/developer/tester),
        # daher mehr Wallclock-Budget. Single-Agent-Runs reichen 10 Min.
        if timeout_s is None:
            self.timeout_s = 1500 if multi_agent else DEFAULT_TIMEOUT_S
        else:
            self.timeout_s = timeout_s

    def propose(
        self,
        *,
        worktree: Path,
        prompt: str,
        max_turns: int,
        budget_usd: Decimal,
        model: str | None = None,
        allowed_tools: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ProposalResult:
        # Multi-Agent: Subagent-Markdowns in den Worktree kopieren, bevor
        # Claude Code startet — dann werden sie via .claude/agents/ entdeckt.
        if self.multi_agent:
            _install_subagents(worktree)
            allowed_tools = _augment_tools_for_multi_agent(allowed_tools)

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "json",
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            self.permission_mode,
        ]
        if self.multi_agent:
            cmd.extend(["--append-system-prompt", ORCHESTRATOR_SYSTEM_PROMPT])
        if allowed_tools:
            cmd.extend(["--allowedTools", allowed_tools])
        chosen_model = model or self.default_model
        if chosen_model:
            cmd.extend(["--model", chosen_model])

        run_env: dict[str, str] = dict(os.environ)
        if env:
            run_env.update(env)
        # Auth: entweder ANTHROPIC_API_KEY in env ODER claude ist via
        # `claude /login` mit einem Subscription-Account angemeldet.
        # forge entscheidet das nicht — wenn beides fehlt, schlägt der
        # claude-Subprozess mit klarem Error fehl, den wir hier propagieren.

        # venv-Auto-Detection: claude führt im Worktree pytest/black/python
        # via Bash-Tool aus. Damit die in der Projekt-venv landen statt System-
        # Python, prependen wir <repo>/.venv/Scripts in PATH.
        run_env = venv_aware_env(Path(worktree), run_env)

        # Capture base SHA, damit wir hinterher den Diff bauen können
        base_sha = _git(worktree, "rev-parse", "HEAD")

        popen_kwargs: dict = dict(
            cwd=str(worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=run_env,
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        start = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as exc:
            raise CodingAgentError(
                f"claude binary not found: {self.claude_bin!r}. "
                "Install Claude Code CLI or set claude_bin to a valid path."
            ) from exc

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            raise CodingAgentTimeout(
                f"claude exceeded {self.timeout_s}s wallclock budget"
            )

        raw = _parse_json_output(stdout)

        # `error_max_turns`/`tool_use` sind Soft-Fails: claude hat das
        # turn-Limit erreicht, aber im Worktree LIEGEN evtl. fertige
        # Änderungen. Wir verwerfen die nicht — der Runner soll sie wie
        # einen normalen Vorschlag validieren und evaluieren.
        soft_fail_subtypes = {"error_max_turns", "error_during_execution"}
        is_soft_fail = (
            proc.returncode != 0
            and raw.get("subtype") in soft_fail_subtypes
        )
        if proc.returncode != 0 and not is_soft_fail:
            stderr_clean = stderr.strip()
            stdout_clean = stdout.strip()
            detail_parts = [p for p in [stderr_clean, stdout_clean] if p]
            detail = " | ".join(detail_parts) if detail_parts else "(no output)"
            raise CodingAgentError(
                f"claude exited {proc.returncode}: {detail[:800]}"
            )
        diff = _git(worktree, "diff", base_sha, "HEAD")
        # Für noch nicht committete Änderungen ergänzen
        wt_diff = _git(worktree, "diff")
        if wt_diff.strip():
            diff = (diff + ("\n" if diff and not diff.endswith("\n") else "") + wt_diff)

        usage = raw.get("usage") or {}
        cost = _extract_cost(raw)

        # Soft-fail subtype trumps any stop_reason claude reports.
        # `error_max_turns` ist deutlicher als `tool_use` für die Telemetrie.
        if raw.get("subtype") == "error_max_turns":
            stop_reason = "max_turns"
        elif raw.get("subtype") == "error_during_execution":
            stop_reason = "error"
        else:
            stop_reason = str(raw.get("stop_reason") or "unknown")

        return ProposalResult(
            diff=diff,
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            cost_usd=cost,
            stop_reason=stop_reason,
            model=str(raw.get("model") or chosen_model or ""),
            model_version=str(raw.get("model") or ""),
            turns_used=int(raw.get("num_turns", 0) or 0),
            duration_ms=duration_ms,
            raw_response=raw,
            error=None if proc.returncode == 0 else f"exit {proc.returncode}, subtype={raw.get('subtype')}",
        )


# --- Helpers ----------------------------------------------------------


def _install_subagents(worktree: Path) -> None:
    """Kopiert die Subagent-Markdowns nach `<worktree>/.claude/agents/`.

    Idempotent: existierende gleichnamige Files werden überschrieben.
    Damit hat Claude Code projektspezifische Subagents zur Verfügung.
    """
    target = worktree / ".claude" / "agents"
    target.mkdir(parents=True, exist_ok=True)
    for src in list_templates():
        shutil.copyfile(src, target / src.name)


def _augment_tools_for_multi_agent(allowed_tools: str | None) -> str:
    """Sicherstellen, dass `Task` in der Allowlist ist — der Master-Claude
    spawnt Subagents über das Task-Tool.

    Wenn `allowed_tools` leer ist, fügen wir Task als einzigen Eintrag.
    """
    if not allowed_tools:
        return "Task"
    if "Task" in allowed_tools:
        return allowed_tools
    return f"{allowed_tools},Task"


def _git(worktree: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(worktree),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        raise CodingAgentError(
            f"git {args} failed in {worktree}: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _parse_json_output(stdout: str) -> dict[str, Any]:
    """Claude CLI gibt mit `--output-format json` ein einziges JSON-Objekt
    auf stdout. Falls es Setup-Logs davor gab, suchen wir das letzte `{…}`."""
    text = stdout.strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        last_open = text.rfind("{")
        last_close = text.rfind("}")
        if last_open == -1 or last_close <= last_open:
            return {}
        try:
            data = json.loads(text[last_open : last_close + 1])
        except json.JSONDecodeError:
            return {}
    return data if isinstance(data, dict) else {}


def _extract_cost(raw: dict[str, Any]) -> Decimal:
    """Liest die Cost-Information aus der Claude-CLI-Antwort.

    Format-Schwankungen über Versionen werden defensiv gehandhabt: erst
    `total_cost_usd` (neuere CLI-Versionen), dann `cost_usd` als Fallback.
    """
    for key in ("total_cost_usd", "cost_usd"):
        if key in raw:
            try:
                return Decimal(str(raw[key]))
            except (ValueError, TypeError):
                continue
    return Decimal("0")


# Type-Check: sicherstellen, dass die Klasse das Protocol erfüllt
_: type[CodingAgent] = ClaudeCodeCLIAgent
