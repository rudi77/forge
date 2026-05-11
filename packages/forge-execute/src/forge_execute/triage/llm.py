"""LLM-basierter Triager via Claude Code CLI (Headless).

Ein einziger ``claude -p``-Aufruf mit eng zugeschnittenem Toolset:

* ``Read`` für lokale Files (CHANGELOG, README, etc.)
* ``Bash(gh issue list:*)``, ``Bash(gh issue view:*)``,
  ``Bash(gh pr list:*)``, ``Bash(gh pr view:*)`` für GitHub-Lookup
* ``Bash(git log:*)``, ``Bash(git diff:*)`` für Repo-History

Kein ``Edit``/``Write`` — Triage darf nichts modifizieren. Output
ist JSON, das wir gegen ``TriageDecision`` validieren.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_adapters.github import ReadyIssue

from forge_execute.triage.base import TriageError, TriageResult

_IS_WINDOWS = platform.system() == "Windows"


_TRIAGE_ALLOWED_TOOLS = ",".join([
    "Read",
    "Bash(gh issue list:*)",
    "Bash(gh issue view:*)",
    "Bash(gh pr list:*)",
    "Bash(gh pr view:*)",
    "Bash(git log:*)",
    "Bash(git diff:*)",
    "Bash(git show:*)",
    "Bash(git status:*)",
])


_TRIAGE_PROMPT_TEMPLATE = """\
Du bist forges Issue-Triager. Du prüfst EIN GitHub-Issue gegen den
aktuellen Repo-Stand und entscheidest, ob ein voller Coding-Run sich
noch lohnt.

Issue #{number} — {title}

{body}

---

Aufgabe: Klassifiziere als genau eine dieser Decisions:

- "relevant" — das Problem existiert noch, ein Fix lohnt sich. (Default,
  wenn du unsicher bist.)
- "stale" — das Issue beschreibt einen Zustand, der nicht mehr stimmt:
  Code, der nicht mehr existiert, ein Verhalten, das längst anders ist,
  oder eine Anforderung, die obsolet ist. Begründe mit konkreten
  Referenzen aus git log / git diff.
- "duplicate" — ein anderes OFFENES Issue oder ein OFFENER PR
  adressiert exakt dasselbe Problem. Du musst dann ``related_pr``
  (oder eine Issue-Nummer als ``related_pr`` einsetzen) angeben.
- "already_solved" — der Fix ist bereits im main-Branch gelandet
  (in einem Commit, der das Issue nicht via "Closes #N"-Trailer
  abgehakt hat). Du musst ``related_commit`` mit dem Short-SHA
  angeben.

Nutze die verfügbaren Tools für Lookups. Bleibe unter {max_turns} Turns.

Antworte am Ende mit GENAU einem JSON-Block in einem ```json...```
Code-Fence:

```json
{{
  "decision": "relevant" | "stale" | "duplicate" | "already_solved",
  "reason": "kurze Begründung, max 500 Zeichen, deutsch",
  "related_pr": null oder Integer,
  "related_commit": null oder String mit Short-SHA
}}
```

Wenn du unsicher bist: ``decision: "relevant"``. Bessere False
Positives bei "Worktree öffnen" als bei "Issue zu Unrecht schließen".
"""


class LLMTriager:
    """Klassifiziert ein Issue per Claude-CLI-Aufruf.

    Bewusst flach: kein Mehr-Turn-Pattern, kein Sub-Agent. Ein Call,
    eine Entscheidung. Wer mehr Komplexität braucht, schreibt einen
    eigenen Triager.
    """

    def __init__(
        self,
        *,
        claude_bin: str = "claude",
        model: str | None = None,
        max_turns: int = 4,
        timeout_s: int = 180,
        permission_mode: str = "bypassPermissions",
    ) -> None:
        self.claude_bin = claude_bin
        self.model = model
        self.max_turns = max_turns
        self.timeout_s = timeout_s
        self.permission_mode = permission_mode

    def triage(self, *, issue: ReadyIssue, repo_root: Path) -> TriageResult:
        prompt = _TRIAGE_PROMPT_TEMPLATE.format(
            number=issue.number,
            title=issue.title,
            body=issue.body or "(no body)",
            max_turns=self.max_turns,
        )

        cmd = [
            self.claude_bin,
            "-p",
            prompt,
            "--output-format", "json",
            "--max-turns", str(self.max_turns),
            "--permission-mode", self.permission_mode,
            "--allowedTools", _TRIAGE_ALLOWED_TOOLS,
        ]
        if self.model:
            cmd.extend(["--model", self.model])

        popen_kwargs: dict[str, Any] = dict(
            cwd=str(repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=dict(os.environ),
        )
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        start = time.monotonic()
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except FileNotFoundError as exc:
            raise TriageError(
                f"claude binary not found: {self.claude_bin!r}"
            ) from exc

        try:
            stdout, stderr = proc.communicate(timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise TriageError(
                f"claude exceeded {self.timeout_s}s triage budget"
            ) from None
        duration_ms = int((time.monotonic() - start) * 1000)
        del duration_ms  # informativ, in v1 nicht im Event

        if proc.returncode != 0:
            stderr_clean = (stderr or "").strip()
            raise TriageError(
                f"claude exited {proc.returncode} during triage: "
                f"{stderr_clean[:400] or '<no stderr>'}"
            )

        raw = _parse_claude_json(stdout)
        result_text = str(raw.get("result") or "")
        decision_blob = _extract_decision_json(result_text)
        if decision_blob is None:
            raise TriageError(
                "triage output did not contain a parseable JSON decision block"
            )

        decision = decision_blob.get("decision")
        if decision not in {"relevant", "stale", "duplicate", "already_solved"}:
            raise TriageError(f"invalid triage decision: {decision!r}")

        usage = raw.get("usage") or {}
        cost = _decimal_from_any(raw.get("total_cost_usd"))
        del usage  # tokens nicht im TriageResult — bleibt im RawResponse

        return TriageResult(
            decision=decision,  # type: ignore[arg-type]
            reason=str(decision_blob.get("reason") or "")[:2000],
            related_pr=_maybe_int(decision_blob.get("related_pr")),
            related_commit=_maybe_str(decision_blob.get("related_commit")),
            turns_used=int(raw.get("num_turns") or 0),
            cost_usd=cost,
            model=str(raw.get("model") or self.model or ""),
        )


# --- Parsing helpers ---------------------------------------------------


def _parse_claude_json(stdout: str) -> dict[str, Any]:
    stripped = stdout.strip()
    if not stripped:
        raise TriageError("claude produced no stdout")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise TriageError(f"claude output is not valid JSON: {exc}") from exc


def _extract_decision_json(result_text: str) -> dict[str, Any] | None:
    """Findet den letzten ```json…```-Block im Claude-Output und parst ihn."""
    needle = "```json"
    last = result_text.rfind(needle)
    if last == -1:
        return None
    after = result_text[last + len(needle) :]
    end = after.find("```")
    if end == -1:
        return None
    blob = after[:end].strip()
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _maybe_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _maybe_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _decimal_from_any(value: Any) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")
