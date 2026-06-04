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
    ReviewResult,
)
from forge_execute.agents.templates import (
    DEFAULT_AGENTS,
    build_orchestrator_prompt,
    extract_plan_from_master_output,
    list_templates,
    normalize_agents,
    roster_needs_orchestration,
)
from forge_execute.evaluators.command import _kill_tree

_IS_WINDOWS = platform.system() == "Windows"


# Default-Wallclock-Limit: ein Vielfaches der LLM-Latenz, falls budget_usd
# nicht limitiert. Cost-Caps machen das eigentliche Limit aus.
DEFAULT_TIMEOUT_S = 600

# Read-only Tool-Set für den Judge — niemals Edit/Write. Der Judge
# bewertet nur, er ändert nichts.
JUDGE_ALLOWED_TOOLS = ",".join([
    "Read",
    "Grep",
    "Glob",
    "Bash(git diff:*)",
    "Bash(git log:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
])


_JUDGE_PROMPT_TEMPLATE = """\
Du bist forges Judge. Du bewertest, ob ein Code-Diff die Akzeptanz-
kriterien eines Issues erfüllt. Du änderst NICHTS — du liest und urteilst.

## Akzeptanzkriterien (vom Menschen geschrieben)

{acceptance}

## Der zu bewertende Diff

```diff
{diff}
```

---

Aufgabe: Bewerte, wie vollständig und korrekt der Diff die
Akzeptanzkriterien erfüllt. Nutze die read-only Tools (Read, Grep,
git diff/log/show), um den Kontext der geänderten Stellen zu prüfen,
falls der Diff allein nicht reicht. Bleibe unter {max_turns} Turns.

Bewertungsmaßstab für ``judge_score`` (0.0 - 1.0):
- 1.0 — alle Kriterien vollständig und korrekt erfüllt.
- 0.7-0.9 — im Kern erfüllt, kleinere Lücken oder Stilfragen.
- 0.4-0.6 — teilweise erfüllt, wesentliche Aspekte fehlen.
- 0.0-0.3 — Kriterien nicht erfüllt, falsch, oder am Thema vorbei.

``verdict`` ist ``"pass"`` ab dem Schwellwert, den der Operator als Gate
gesetzt hat — gib im Zweifel ``"fail"`` (besser eine ehrliche
Ablehnung als ein unverdientes Durchwinken).

Antworte am Ende mit GENAU einem JSON-Block in einem ```json...```
Code-Fence:

```json
{{
  "judge_score": 0.0 bis 1.0,
  "verdict": "pass" | "fail",
  "reasoning": "kurze Begründung, max 800 Zeichen, deutsch"
}}
```
"""


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
        agents: list[str] | None = None,
    ) -> None:
        self.claude_bin = claude_bin
        self.default_model = default_model
        self.permission_mode = permission_mode

        # Roster-Auflösung (Spec v0.3 Teil 5.1): `agents` ist die Quelle der
        # Wahrheit, sobald gesetzt. Sonst bildet das `multi_agent`-Flag den
        # Alt-Pfad ab — True = volles Default-Roster, False = einsamer
        # developer (klassischer Single-Agent-Run, kein Plan, kein Task-Tool).
        if agents is not None:
            self.agents = normalize_agents(agents)
        elif multi_agent:
            self.agents = list(DEFAULT_AGENTS)
        else:
            self.agents = ["developer"]
        self.multi_agent = roster_needs_orchestration(self.agents)

        # Multi-Agent-Runs spawnen Subagents (architect/developer/tester),
        # daher mehr Wallclock-Budget. Single-Agent-Runs reichen 10 Min.
        if timeout_s is None:
            self.timeout_s = 1500 if self.multi_agent else DEFAULT_TIMEOUT_S
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
        # Multi-Agent: nur die Subagent-Markdowns des aktiven Rosters in den
        # Worktree kopieren, bevor Claude Code startet — dann werden sie via
        # .claude/agents/ entdeckt.
        if self.multi_agent:
            _install_subagents(worktree, agents=self.agents)
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
            cmd.extend(
                ["--append-system-prompt", build_orchestrator_prompt(self.agents)]
            )
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

        # Plan-Extraktion: nur wenn der architect im Roster ist, aus dem
        # result-Text der Master-claude-Antwort. Ohne architect → kein Plan.
        plan_md: str | None = None
        if "architect" in self.agents:
            result_text = str(raw.get("result") or "")
            plan_md = extract_plan_from_master_output(result_text)

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
            plan_md=plan_md,
        )

    def review(
        self,
        *,
        worktree: Path,
        acceptance_criteria: str,
        diff: str,
        max_turns: int,
        budget_usd: Decimal,
        model: str | None = None,
        allowed_tools: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ReviewResult:
        """Bewertet den Diff gegen die Akzeptanzkriterien (read-only).

        Ein einziger ``claude -p``-Aufruf mit einem read-only Tool-Set.
        Bei Subprozess-Fehler, Timeout oder unparsbarem JSON wird
        ``CodingAgentError``/``CodingAgentTimeout`` geworfen — der Caller
        (JudgeEvaluator) behandelt das fail-closed.
        """
        prompt = _JUDGE_PROMPT_TEMPLATE.format(
            acceptance=acceptance_criteria.strip() or "(keine Kriterien angegeben)",
            diff=_truncate_diff(diff),
            max_turns=max_turns,
        )
        tools = allowed_tools or JUDGE_ALLOWED_TOOLS

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--permission-mode", self.permission_mode,
            "--allowedTools", tools,
        ]
        chosen_model = model or self.default_model
        if chosen_model:
            cmd.extend(["--model", chosen_model])

        run_env: dict[str, str] = dict(os.environ)
        if env:
            run_env.update(env)
        run_env = venv_aware_env(Path(worktree), run_env)

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

        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            try:
                proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
            raise CodingAgentTimeout(
                f"claude exceeded {self.timeout_s}s wallclock budget during review"
            ) from None
        duration_ms = int((time.monotonic() - start) * 1000)

        if proc.returncode != 0:
            stderr_clean = (stderr or "").strip()
            raise CodingAgentError(
                f"claude exited {proc.returncode} during review: "
                f"{stderr_clean[:400] or '<no stderr>'}"
            )

        raw = _parse_json_output(stdout)
        result_text = str(raw.get("result") or "")
        verdict_blob = _extract_json_block(result_text)
        if verdict_blob is None:
            raise CodingAgentError(
                "judge output did not contain a parseable JSON verdict block"
            )

        score = _clamp_unit(verdict_blob.get("judge_score"))
        if score is None:
            raise CodingAgentError(
                f"judge returned no usable judge_score: {verdict_blob.get('judge_score')!r}"
            )
        verdict = verdict_blob.get("verdict")
        if verdict not in {"pass", "fail"}:
            # Verdict ableiten ist unsicher — wir verlassen uns auf das Gate,
            # nicht auf das Self-Verdict. Normalisiere defensiv.
            verdict = "pass" if score >= 0.8 else "fail"

        usage = raw.get("usage") or {}
        return ReviewResult(
            judge_score=score,
            verdict=verdict,  # type: ignore[arg-type]
            reasoning=str(verdict_blob.get("reasoning") or "")[:2000],
            tokens_in=int(usage.get("input_tokens", 0) or 0),
            tokens_out=int(usage.get("output_tokens", 0) or 0),
            cost_usd=_extract_cost(raw),
            turns_used=int(raw.get("num_turns", 0) or 0),
            duration_ms=duration_ms,
            model=str(raw.get("model") or chosen_model or ""),
            raw_response=raw,
        )


# --- Helpers ----------------------------------------------------------


def _install_subagents(worktree: Path, agents: list[str] | None = None) -> None:
    """Kopiert die Subagent-Markdowns nach `<worktree>/.claude/agents/`.

    Hybrid-Lookup (Spec v0.3 Teil 6.5, Designentscheidung 5.1):
    1. Projekt-Override unter `<repo-root>/.forge/agents/<name>.md` hat
       Vorrang
    2. forge-Defaults aus forge_execute.agents.templates füllen die
       restlichen Subagents auf

    Identifikation per Datei-Basename: `architect.md` aus dem Projekt
    überschreibt `architect.md` aus den Defaults, andere Defaults bleiben.

    `agents` begrenzt, welche Rollen installiert werden — nur die im Roster
    aktivierten Arbeitspferde landen im Worktree. `None` installiert alle
    (Rückwärtskompatibilität). Der Filter gilt auch für Projekt-Overrides.

    Idempotent: existierende Files in `.claude/agents/` werden überschrieben.
    """
    target = worktree / ".claude" / "agents"
    target.mkdir(parents=True, exist_ok=True)

    wanted: set[str] | None = (
        {a.strip().lower() for a in agents} if agents is not None else None
    )

    def _in_roster(filename: str) -> bool:
        return wanted is None or Path(filename).stem.lower() in wanted

    # Projekt-Override-Verzeichnis aufwärts vom Worktree finden — analog
    # zur venv-Auto-Detection. forge-Worktrees liegen unter
    # `<repo>/.forge/worktrees/<id>/`, der Repo-Root hat das `.forge/agents/`.
    project_overrides: dict[str, Path] = {}
    for ancestor in [worktree, *worktree.parents]:
        candidate = ancestor / ".forge" / "agents"
        if candidate.is_dir():
            for md in candidate.glob("*.md"):
                if _in_roster(md.name):
                    project_overrides.setdefault(md.name, md)
            break  # ersten Treffer aufwärts nehmen — kein Suchen weiter

    # Erst Defaults legen, dann Projekt-Overrides drüberkopieren.
    for src in list_templates(agents):
        shutil.copyfile(src, target / src.name)
    for name, src in project_overrides.items():
        shutil.copyfile(src, target / name)


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


def _extract_json_block(result_text: str) -> dict[str, Any] | None:
    """Findet den letzten ```json…```-Block im Claude-Output und parst ihn.

    Fällt auf ein nacktes letztes ``{…}`` zurück, falls kein Code-Fence
    da ist — manche Modelle vergessen den Fence."""
    needle = "```json"
    last = result_text.rfind(needle)
    if last != -1:
        after = result_text[last + len(needle) :]
        end = after.find("```")
        if end != -1:
            try:
                parsed = json.loads(after[:end].strip())
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
    # Fallback: letztes {…}
    last_open = result_text.rfind("{")
    last_close = result_text.rfind("}")
    if last_open == -1 or last_close <= last_open:
        return None
    try:
        parsed = json.loads(result_text[last_open : last_close + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _clamp_unit(value: Any) -> float | None:
    """Parst einen Wert nach float und klemmt ihn auf [0, 1]. None bei Garbage."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, f))


def _truncate_diff(diff: str, *, max_chars: int = 24000) -> str:
    """Begrenzt den Diff für den Judge-Prompt, damit große Diffs das
    Token-Budget nicht sprengen. Mitte wird elidiert, Anfang+Ende bleiben."""
    if len(diff) <= max_chars:
        return diff
    head = diff[: max_chars * 2 // 3]
    tail = diff[-max_chars // 3 :]
    return f"{head}\n\n... [Diff gekürzt, {len(diff)} Zeichen gesamt] ...\n\n{tail}"


# Type-Check: sicherstellen, dass die Klasse das Protocol erfüllt
_: type[CodingAgent] = ClaudeCodeCLIAgent
