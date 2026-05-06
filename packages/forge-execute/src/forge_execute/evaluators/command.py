"""Command-Evaluator.

Führt einen Shell-Command gemäß `EvalSuiteConfig` aus, honoriert `budget_s`
als Timeout, parst stdout zu numerischen Messungen, und sammelt
Tool-Versionen für den Replay-Kontrakt (Spec Teil 4.1, 6.4).

Drei Parse-Modi:

- `pytest_json`: erkennt die Pytest-Summary-Line ("5 passed in 0.5s",
  "2 failed, 3 passed", "1 error") und liefert
  `pytest_pass_rate`, `pytest_test_count`, `pytest_failed_count`.
- `scores_json`: erwartet, dass stdout ein JSON-Dict mit Float-Werten ist,
  z.B. `{"coverage_pct": 87.2, "p95_latency_ms": 412.0}`.
- `raw`: kein Parsing, `measurements` bleibt leer.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from forge_core.spec import EvalSuiteConfig

_IS_WINDOWS = platform.system() == "Windows"


# Standard-Tools, deren Version für den Replay-Kontrakt mitgespeichert wird.
DEFAULT_TOOL_VERSION_PROBES: tuple[tuple[str, list[str]], ...] = (
    ("python", [sys.executable, "--version"]),
    ("pytest", ["pytest", "--version"]),
    ("ruff", ["ruff", "--version"]),
    ("mypy", ["mypy", "--version"]),
    ("node", ["node", "--version"]),
    ("npm", ["npm", "--version"]),
    ("git", ["git", "--version"]),
)


@dataclass(frozen=True)
class EvalRunResult:
    """Ergebnis eines Eval-Laufs.

    `measurements` enthält die geparsten numerischen Werte; daraus baut der
    Runner den `EvalFinishedPayload` mit Gates, Scores und Diagnostics.
    """

    suite_id: str
    exit_code: int
    duration_ms: int
    timeout: bool
    measurements: dict[str, float] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    tool_versions: dict[str, str] = field(default_factory=dict)

    @property
    def success(self) -> bool:
        """True nur, wenn weder Timeout noch Non-Zero-Exit."""
        return self.exit_code == 0 and not self.timeout


class CommandEvaluator:
    """Führt eine Eval-Suite aus.

    Verwendung::

        evaluator = CommandEvaluator()
        result = evaluator.run(suite=spec.eval_suites["quick"], cwd=worktree.path)
        # result.measurements -> dict für Gates/Scores
        # result.tool_versions -> dict für tool_versions-Artefakt
    """

    def __init__(
        self,
        *,
        tool_probes: tuple[tuple[str, list[str]], ...] = DEFAULT_TOOL_VERSION_PROBES,
    ) -> None:
        self.tool_probes = tool_probes

    def run(
        self,
        *,
        suite_id: str,
        suite: EvalSuiteConfig,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> EvalRunResult:
        cmd_str = suite.cmd
        timeout_s = suite.budget_s

        # `shell=True` bewusst — die Spec erlaubt Pipes und `cd backend && pytest`
        # Konstrukte, die ein Shell-Layer brauchen. Sicherheitskontext: der
        # Command kommt aus der project.yaml, die der Operator selbst geschrieben
        # hat — nicht aus User-Input des Issues.
        start = time.monotonic()
        timeout = False
        exit_code = -1

        popen_kwargs: dict = dict(
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        # Auf beiden Plattformen brauchen wir eine Möglichkeit, den ganzen
        # Prozessbaum zu killen, nicht nur den Shell-Wrapper.
        if _IS_WINDOWS:
            popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            popen_kwargs["start_new_session"] = True

        proc = subprocess.Popen(cmd_str, **popen_kwargs)
        try:
            stdout, stderr = proc.communicate(timeout=timeout_s)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            timeout = True
            _kill_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout, stderr = proc.communicate()
            exit_code = 124  # Konvention für Timeout
        duration_ms = int((time.monotonic() - start) * 1000)

        measurements = self._parse(suite, stdout, exit_code)

        return EvalRunResult(
            suite_id=suite_id,
            exit_code=exit_code,
            duration_ms=duration_ms,
            timeout=timeout,
            measurements=measurements,
            stdout=stdout,
            stderr=stderr,
            tool_versions=self.collect_tool_versions(),
        )

    def _parse(
        self,
        suite: EvalSuiteConfig,
        stdout: str,
        exit_code: int,
    ) -> dict[str, float]:
        if suite.parses == "pytest_json":
            return parse_pytest_output(stdout, exit_code=exit_code)
        if suite.parses == "scores_json":
            return parse_scores_json(stdout)
        return {}

    def collect_tool_versions(self) -> dict[str, str]:
        """Probt jedes Tool aus `tool_probes` mit `--version`.

        Tools, die nicht im PATH stehen, werden ignoriert (`""`). So bleibt
        das Artefakt deterministisch über die Maschine — wer Tools nachzieht,
        sieht in den Versionen, was zum Eval-Zeitpunkt da war.
        """
        result: dict[str, str] = {}
        for name, probe_cmd in self.tool_probes:
            try:
                proc = subprocess.run(
                    probe_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    encoding="utf-8",
                    errors="replace",
                )
                version = (proc.stdout or proc.stderr).strip().splitlines()[0]
                result[name] = version
            except (FileNotFoundError, subprocess.TimeoutExpired, IndexError, OSError):
                # Tool nicht da oder timeout — wir lassen es stumm aus.
                continue
        return result


# --- Process-Tree-Termination ------------------------------------------


def _kill_tree(pid: int) -> None:
    """Killt den ganzen Subprozess-Baum eines Shell-Aufrufs.

    Ohne diese Logik überlebt das Enkel-Python einen `subprocess.run(..., shell=True)`
    Timeout — Shell wird gekillt, das eigentliche Eval-Tool nicht.
    """
    if _IS_WINDOWS:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
    else:
        import contextlib
        import signal

        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)


# --- Parser -------------------------------------------------------------


# pytest summary line examples:
#   "5 passed in 0.45s"
#   "2 failed, 3 passed in 0.45s"
#   "5 failed in 0.45s"
#   "1 error"
#   "no tests ran"
_PYTEST_SUMMARY_RE = re.compile(
    r"(?:^|\s)"
    r"(?:(?P<failed>\d+) failed)?"
    r"(?:,?\s*(?P<passed>\d+) passed)?"
    r"(?:,?\s*(?P<errors>\d+) errors?)?"
    r"(?:,?\s*(?P<skipped>\d+) skipped)?"
    r"(?:,?\s*\d+ xfailed)?"
    r"(?:,?\s*\d+ xpassed)?"
    r"(?:,?\s*\d+ warnings?)?"
    r"\s*(?:in [\d.]+s)?\s*=*\s*$",
    re.MULTILINE,
)


def parse_pytest_output(stdout: str, *, exit_code: int) -> dict[str, float]:
    """Parst die pytest-Summary-Line.

    Suchfeld ist die letzte nicht-leere Zeile, die mit '=' eingerahmt sein kann
    (`=== 5 passed in 0.5s ===`). Wir ziehen die Zahlen heraus und berechnen
    `pytest_pass_rate = passed / (passed + failed + errors)`.

    Bei `no tests ran` liefern wir `pytest_pass_rate=1.0` (vacuously true).
    """
    if "no tests ran" in stdout.lower():
        return {
            "pytest_pass_rate": 1.0,
            "pytest_test_count": 0.0,
            "pytest_failed_count": 0.0,
        }

    # Suche letzte aussagekräftige Zeile rückwärts
    lines = [line.strip(" =") for line in stdout.splitlines() if line.strip(" =")]
    for line in reversed(lines):
        match = _PYTEST_SUMMARY_RE.search(line)
        if not match:
            continue
        passed = int(match.group("passed") or 0)
        failed = int(match.group("failed") or 0)
        errors = int(match.group("errors") or 0)
        total = passed + failed + errors
        if total == 0:
            continue  # leere Match
        return {
            "pytest_pass_rate": passed / total,
            "pytest_test_count": float(total),
            "pytest_failed_count": float(failed + errors),
        }

    # Fallback: nur Exit-Code
    return {
        "pytest_pass_rate": 1.0 if exit_code == 0 else 0.0,
        "pytest_test_count": 0.0,
        "pytest_failed_count": 0.0 if exit_code == 0 else 1.0,
    }


def parse_scores_json(stdout: str) -> dict[str, float]:
    """Erwartet ein JSON-Dict mit Float-Werten als stdout.

    Wenn der Eval-Befehl mehr als nur das JSON druckt (z.B. Setup-Logs vor
    dem JSON), suchen wir nach dem letzten `{` … `}` im Output.
    """
    text = stdout.strip()
    if not text:
        return {}

    # Versuche zuerst direktes Parse
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Suche letztes {…} block
        last_open = text.rfind("{")
        last_close = text.rfind("}")
        if last_open == -1 or last_close == -1 or last_close < last_open:
            return {}
        try:
            data = json.loads(text[last_open : last_close + 1])
        except json.JSONDecodeError:
            return {}

    if not isinstance(data, dict):
        return {}

    out: dict[str, float] = {}
    for key, value in data.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[str(key)] = float(value)
    return out
