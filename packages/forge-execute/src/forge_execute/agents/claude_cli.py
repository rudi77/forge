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

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from forge_execute._venv import venv_aware_env
from forge_execute.agents.base import (
    CodingAgent,
    CodingAgentError,
    CodingAgentRateLimited,
    CodingAgentTimeout,
    ProposalResult,
    ReviewResult,
)
from forge_execute.agents.templates import (
    DEFAULT_AGENTS,
    build_orchestrator_prompt,
    extract_agents_from_master_output,
    extract_lessons_block,
    extract_plan_from_master_output,
    list_templates,
    normalize_agents,
    roster_needs_orchestration,
)
from forge_execute.evaluators.command import _kill_tree

_IS_WINDOWS = platform.system() == "Windows"

_log = logging.getLogger(__name__)

# Interaktive Tools, die im Headless-Mode (`claude -p`) nie funktionieren
# können: der Permission-Prompt wird automatisch verworfen, der Versuch
# kostet aber einen vollen API-Round-Trip (beobachtet: architect stellt
# AskUserQuestion, Frage wird verworfen, der Default gilt sowieso).
# Hartes Deny via `--disallowedTools` — wirkt auch unter bypassPermissions,
# wo `--allowedTools` allein nichts ausschließt.
HEADLESS_DISALLOWED_TOOLS = "AskUserQuestion"


# Default-Wallclock-Limit: ein Vielfaches der LLM-Latenz, falls budget_usd
# nicht limitiert. Cost-Caps machen das eigentliche Limit aus.
DEFAULT_TIMEOUT_S = 600
# Multi-Agent-Orchestrierung (architect→developer→tester→reviewer) braucht
# deutlich mehr Wallclock als ein Single-Agent-Run. 1 h Default, per
# `--agent-timeout` überschreibbar.
MULTI_AGENT_TIMEOUT_S = 3600

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

        # Multi-Agent-Runs spawnen Subagents (architect/developer/tester/
        # reviewer) und orchestrieren sie sequentiell — das volle Team gegen ein
        # Greenfield-Workpaket sprengt 25 Min locker (Eval/Judge/Reviewer wurden
        # sonst gekappt, obwohl der Developer fertig war). Default daher 1 h; per
        # `timeout_s` (CLI: `--agent-timeout`) überschreibbar. Single-Agent-Runs
        # reichen 10 Min.
        if timeout_s is not None and timeout_s > 0:
            self.timeout_s = timeout_s
        else:
            self.timeout_s = MULTI_AGENT_TIMEOUT_S if self.multi_agent else DEFAULT_TIMEOUT_S

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
        resume_session_id: str | None = None,
    ) -> ProposalResult:
        # Multi-Agent: nur die Subagent-Markdowns des aktiven Rosters in den
        # Worktree kopieren, bevor Claude Code startet — dann werden sie via
        # .claude/agents/ entdeckt.
        if self.multi_agent:
            _install_subagents(worktree, agents=self.agents)
            allowed_tools = _augment_tools_for_multi_agent(
                allowed_tools, self.agents
            )

        # stream-json statt json: claude emittiert pro Event eine JSON-Zeile
        # (system/init, assistant inkl. tool_use-Blöcke, user/tool_results,
        # result am Ende). Wir schreiben jede Zeile live + zeitgestempelt in
        # ein Log unter .forge/logs/<run_id>/ — damit ist die Propose-Phase
        # keine Blackbox mehr (`forge watch` zeigt daraus die Agent-Aktivität;
        # `--verbose` ist im Print-Mode Pflicht für stream-json). Das finale
        # result-Event hat exakt die Shape des alten json-Outputs.
        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            self.permission_mode,
            "--disallowedTools",
            HEADLESS_DISALLOWED_TOOLS,
        ]
        # Resume nach Usage-Limit (Inkr. 2): claude setzt die frühere Session
        # mit vollem Kontext fort; der `prompt` ist die Continue-Anweisung und
        # der Worktree enthält bereits die Partial-Arbeit des unterbrochenen Laufs.
        if resume_session_id:
            cmd.extend(["--resume", resume_session_id])
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

        log_path = _new_stream_log_path(worktree, label="propose")

        start = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as exc:
            raise CodingAgentError(
                f"claude binary not found: {self.claude_bin!r}. "
                "Install Claude Code CLI or set claude_bin to a valid path."
            ) from exc

        lines, stderr, timed_out = _pump_streaming(
            proc, timeout_s=self.timeout_s, log_path=log_path
        )
        duration_ms = int((time.monotonic() - start) * 1000)

        if timed_out:
            raise CodingAgentTimeout(
                f"claude exceeded {self.timeout_s}s wallclock budget "
                f"(stream log: {log_path})"
            )

        raw = _extract_result_event(lines)

        # session_id früh ziehen — sie steht im init-`system`-Event (ab Start
        # vorhanden) und nochmal im result-Event. Resume-Anker für alle Pfade.
        session_id = _extract_session_id(lines) or (raw.get("session_id") or None)

        # Usage-/Session-Limit (HTTP 429) ZUERST prüfen: claude meldet das als
        # result-Event mit subtype "success" — aber is_error=true +
        # api_error_status=429 (irreführend; NICHT über subtype/returncode
        # erkennbar). Eigene Exception mit dem Resume-Anker, damit der Runner den
        # Run als fortsetzbar festhält statt als generischen Fehler mit verlorenem
        # Cost zu verbuchen. Muss VOR dem returncode-Check stehen, sonst greift
        # darunter der generische CodingAgentError.
        if _is_rate_limited(raw):
            result_text = str(raw.get("result") or "").strip()
            raise CodingAgentRateLimited(
                f"claude hit a usage/session limit: {result_text[:200]} "
                f"(stream log: {log_path})",
                session_id=session_id,
                reset_at=_parse_reset_time(result_text),
                cost_usd=_extract_cost(raw),
                turns_used=int(raw.get("num_turns", 0) or 0),
                stream_log=str(log_path),
            )

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
            stdout_tail = "\n".join(lines[-5:]).strip()
            detail_parts = [p for p in [stderr_clean, stdout_tail] if p]
            detail = " | ".join(detail_parts) if detail_parts else "(no output)"
            raise CodingAgentError(
                f"claude exited {proc.returncode}: {detail[:800]} "
                f"(stream log: {log_path})"
            )
        # Neu angelegte (untracked) Files erscheinen weder in `git diff base
        # HEAD` noch in `git diff`. Ohne intent-to-add (`git add -N`) fehlten
        # sie im Proposal-Diff — und damit im Judge-Input, im Capability-Check
        # und im KEEP-Commit (genau der Greenfield-Bug). Wir markieren sie als
        # intent-to-add (staget keinen Inhalt, nur die Existenz), schließen aber
        # die transienten `.claude/`-Subagent-Markdowns aus.
        _stage_untracked_for_diff(worktree)
        # `git diff base_sha` (ohne zweite Ref) vergleicht den Working Tree
        # gegen base — deckt committed + uncommitted + intent-added in einem ab.
        diff = _git(worktree, "diff", base_sha)

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

        # Plan- und Agents-Extraktion aus dem result-Text der Master-claude-
        # Antwort. Plan nur, wenn der architect im Roster ist (ohne architect
        # kein Plan). Die tatsächlich gerufenen Rollen meldet der Master in
        # jedem orchestrierten Run zurück.
        plan_md: str | None = None
        agents_invoked: list[str] | None = None
        lessons_block: str | None = None
        if self.multi_agent:
            result_text = str(raw.get("result") or "")
            if "architect" in self.agents:
                plan_md = extract_plan_from_master_output(result_text)
            agents_invoked = extract_agents_from_master_output(result_text)
            lessons_block = extract_lessons_block(result_text)

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
            agents_invoked=agents_invoked,
            lessons_block=lessons_block,
            stream_log=str(log_path),
            session_id=session_id,
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
            "--disallowedTools", HEADLESS_DISALLOWED_TOOLS,
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
        # Ohne `model:`-Frontmatter erbt der Subagent das Master-Modell —
        # meist das teuerste (opus). Die forge-Defaults tragen alle
        # `model: sonnet`; ein Override ohne die Zeile ist fast immer ein
        # Versehen und der größte Kostentreiber eines Runs.
        if not _frontmatter_has_model(src):
            _log.warning(
                "project agent override %s has no `model:` frontmatter — "
                "the subagent inherits the master model (often the most "
                "expensive one). Add e.g. `model: sonnet` to %s.",
                name,
                src,
            )


def _frontmatter_has_model(path: Path) -> bool:
    """True, wenn die Subagent-Markdown ein ``model:``-Frontmatter-Feld trägt.

    Lesefehler werden als True behandelt — die Funktion speist nur eine
    Warnung, sie darf die Installation nie scheitern lassen.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return True
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    return bool(re.search(r"^model\s*:", text[3:end], re.MULTILINE))


def _augment_tools_for_multi_agent(
    allowed_tools: str | None, agents: list[str] | None = None
) -> str:
    """Allowlist um die Tools ergänzen, die die Orchestrierung braucht.

    - `Task` immer — der Master-Claude spawnt Subagents über das Task-Tool.
    - `Skill(simplify)` nur, wenn `simplify` im Roster steht — der Master ruft
      dann die built-in `/simplify`-Skill auf. Bewusst gescopt (nur diese eine
      Skill), nicht das offene `Skill`: die Allowlist bleibt die enge
      Verteidigungslinie.

    Wenn `allowed_tools` leer ist, bauen wir die Liste von Grund auf.
    """
    parts = [p for p in (allowed_tools.split(",") if allowed_tools else []) if p]
    additions = ["Task"]
    if agents and "simplify" in agents:
        additions.append("Skill(simplify)")
    for tool in additions:
        if tool not in parts:
            parts.append(tool)
    return ",".join(parts)


def _new_stream_log_path(worktree: Path, *, label: str) -> Path:
    """Pfad für das Stream-Log dieses Aufrufs (Verzeichnis wird angelegt).

    Standard-Layout: der Worktree liegt unter ``<repo>/.forge/worktrees/<run_id>/``
    → Log nach ``<repo>/.forge/logs/<run_id>/<label>-<utc>.jsonl``. Bewusst
    AUSSERHALB des Worktrees: ``revert()`` macht ``git clean -fdx`` und würde
    Logs im Worktree bei DISCARD wegblasen — genau dann braucht man sie.
    Fallback für andere Layouts (Tests, direkte Aufrufe): ``<worktree>/.claude/
    forge-logs/`` (von Diff/Commit ohnehin ausgeschlossen).
    """
    wt = Path(worktree).resolve()
    if wt.parent.name == "worktrees" and wt.parent.parent.name == ".forge":
        log_dir = wt.parent.parent / "logs" / wt.name
    else:
        log_dir = wt / ".claude" / "forge-logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")[:-3]
    return log_dir / f"{label}-{stamp}.jsonl"


def _pump_streaming(
    proc: subprocess.Popen[str],
    *,
    timeout_s: int,
    log_path: Path | None,
) -> tuple[list[str], str, bool]:
    """Liest stdout/stderr des laufenden claude-Prozesses zeilenweise.

    Jede stdout-Zeile (= ein stream-json-Event) wird sofort als Envelope
    ``{"ts": <UTC-ISO>, "event": <claude-event>}`` ins Log geschrieben und
    geflusht — `forge watch` kann die Datei live tailen. Timestamps stammen
    von uns (claude-Events tragen keine), damit sichtbar wird, WO die Zeit
    verbraucht wird. Reader laufen in Threads (Windows kennt kein non-blocking
    readline auf Pipes); die Deadline überwacht der Hauptthread via ``wait``.

    Returns:
        (stdout_lines, stderr_text, timed_out) — Zeilen ohne Newline.
    """
    lines: list[str] = []
    stderr_chunks: list[str] = []

    log_file: TextIO | None = None
    if log_path is not None:
        log_file = log_path.open("a", encoding="utf-8")

    def _pump_stdout() -> None:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip("\r\n")
            if not line.strip():
                continue
            lines.append(line)
            if log_file is not None:
                ts = datetime.now(UTC).isoformat(timespec="milliseconds")
                try:
                    event: Any = json.loads(line)
                except json.JSONDecodeError:
                    event = {"type": "raw_text", "text": line}
                log_file.write(json.dumps({"ts": ts, "event": event}) + "\n")
                log_file.flush()

    def _pump_stderr() -> None:
        assert proc.stderr is not None
        for raw_line in proc.stderr:
            stderr_chunks.append(raw_line)

    t_out = threading.Thread(target=_pump_stdout, daemon=True)
    t_err = threading.Thread(target=_pump_stderr, daemon=True)
    t_out.start()
    t_err.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=5)

    # Pipes schließen nach Prozessende → Reader-Threads laufen aus.
    t_out.join(timeout=10)
    t_err.join(timeout=10)
    if log_file is not None:
        log_file.close()

    return lines, "".join(stderr_chunks), timed_out


def _extract_result_event(lines: list[str]) -> dict[str, Any]:
    """Findet das finale ``result``-Event im stream-json-Output.

    Das result-Event hat exakt die Shape des alten ``--output-format json``-
    Outputs (subtype, num_turns, total_cost_usd, usage, result, stop_reason) —
    der restliche propose-Pfad bleibt dadurch unverändert. Rückwärts gesucht,
    weil es konstruktionsbedingt die letzte Zeile ist. Leeres Dict, wenn keins
    existiert (Crash vor Abschluss) — Caller behandelt das wie bisher.
    """
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("type") == "result":
            return obj
    return {}


def _extract_session_id(lines: list[str]) -> str | None:
    """Zieht die claude-``session_id`` aus dem stream-json.

    Bevorzugt das erste Event mit ``session_id`` (das init-``system``-Event steht
    ab Sekunde 1 fest). Resume-Anker für ``claude --resume``; None, wenn keine
    ID im Stream auftaucht (Crash vor dem init-Event).
    """
    for line in lines:
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("session_id"):
            return str(obj["session_id"])
    return None


def _is_rate_limited(raw: dict[str, Any]) -> bool:
    """Erkennt ein Claude-Usage-/Session-Limit (HTTP 429) im result-Event.

    Das Signal ist tückisch: ``subtype`` bleibt ``"success"``, aber
    ``is_error=true`` und ``api_error_status=429``. Fallback über den result-Text
    (``"session limit"`` / ``"usage limit"`` / ``"resets"``), falls eine CLI-
    Version den Status nicht mitliefert. Nur dann True, wenn überhaupt ein Fehler
    vorliegt — ein normaler Erfolg wird nie als rate-limited fehlklassifiziert.
    """
    if not raw.get("is_error"):
        return False
    if raw.get("api_error_status") == 429:
        return True
    text = str(raw.get("result") or "").lower()
    return "session limit" in text or "usage limit" in text or "resets" in text


# "...resets 1pm (Europe/Vienna)" / "resets 09:30 (UTC)" / "resets 1 pm"
_RESET_TIME_RE = re.compile(
    r"resets?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:\(([^)]+)\))?",
    re.IGNORECASE,
)


def _parse_reset_time(text: str, *, now: datetime | None = None) -> datetime | None:
    """Best-effort: parst ``"...resets 1pm (Europe/Vienna)"`` → nächster UTC-Zeitpunkt.

    Liefert den **nächsten** Zeitpunkt mit dieser Lokalzeit (rollt auf morgen,
    wenn die Uhrzeit heute schon vorbei ist). None, wenn der Text nicht parsebar
    ist oder die Zeitzone unbekannt (z. B. fehlendes ``tzdata`` auf Windows) —
    dann muss der Resume manuell ausgelöst werden. ``now`` ist für Tests injizierbar.
    """
    match = _RESET_TIME_RE.search(text or "")
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2) or 0)
    meridiem = (match.group(3) or "").lower()
    tz_name = (match.group(4) or "").strip()
    if meridiem == "pm" and hour != 12:
        hour += 12
    elif meridiem == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    try:
        tz = ZoneInfo(tz_name) if tz_name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return None
    base = (now or datetime.now(UTC)).astimezone(tz)
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target = target + timedelta(days=1)
    return target.astimezone(UTC)


def _stage_untracked_for_diff(worktree: Path) -> None:
    """Markiert untrackte Files via `git add -N` als intent-to-add.

    Damit erscheinen vom Coding-Agent neu angelegte Dateien im nachfolgenden
    `git diff` (intent-to-add staget keinen Blob, nur die Existenz). Die
    transienten Subagent-Markdowns unter `.claude/` bleiben ausgeschlossen —
    sie sind forge-intern und dürfen nie im Diff oder PR landen.
    """
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard")
    paths = [
        line
        for line in untracked.splitlines()
        if line.strip() and not line.startswith(".claude/")
    ]
    if paths:
        _git(worktree, "add", "-N", "--", *paths)


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
