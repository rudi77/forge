"""Capability-Enforcement.

Aus der Spec Teil 5.2:

    erlaubt = (paths ∈ surfaces) ∧ (paths ∉ forbidden) ∧ (action ∈ capabilities)

Forbidden Zones sagen *was* nicht angefasst werden darf, Capabilities sagen
*welche Aktionen* erlaubt sind. Beide Schichten müssen unabhängig grün sein.

`push_to_main`/`push_force` sind hart deaktiviert — schon im Spec-Loader
durchgesetzt; hier nochmal, weil die Capability-Klasse die letzte
Verteidigungslinie ist, bevor eine Aktion stattfindet. `merge_pr` ist seit
dem Agent-Review-Merge opt-in (folgt der Spec-Flag, nicht mehr hart-deny).

Diese Klasse emittiert KEINE Events selbst. Sie liefert ein `CheckResult`
zurück; der Caller (Runner) entscheidet, ob er eine `GuardrailViolation`
emittiert.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forge_core.events import GuardrailViolationPayload
from forge_core.events.kinds.guardrail import GuardrailKind
from forge_core.spec import ProjectSpec
from pathspec import GitIgnoreSpec

CapabilityAction = Literal[
    "commit",
    "open_pr",
    "comment_issue",
    "close_issue",
    "merge_pr",
    "push_to_main",
    "push_force",
]


@dataclass(frozen=True)
class CheckResult:
    """Ergebnis einer Capability-Prüfung."""

    allowed: bool
    guardrail_id: GuardrailKind | None = None
    detail: str | None = None

    @property
    def denied(self) -> bool:
        return not self.allowed

    def to_violation(self, attempted_action: str) -> GuardrailViolationPayload:
        """Konvertiert ein Deny-Ergebnis in den Event-Payload."""
        if self.allowed:
            raise ValueError("CheckResult is allowed; cannot convert to violation")
        assert self.guardrail_id is not None
        return GuardrailViolationPayload(
            guardrail_id=self.guardrail_id,
            attempted_action=attempted_action,
            blocked=True,
            detail=self.detail,
        )


_OK = CheckResult(allowed=True)


class Capabilities:
    """Capability-Enforcer für eine Spec.

    Konstruiert einmal pro Run aus der `ProjectSpec`. Pfad-Matching nutzt
    gitignore-Stil-Glob (`pathspec`), um die in der Spec gezeigten Pattern
    (`backend/migrations/**`, `**/*.py`) korrekt aufzulösen.
    """

    def __init__(self, spec: ProjectSpec) -> None:
        self.spec = spec

        # Forbidden-Patterns als pathspec
        self._forbidden = GitIgnoreSpec.from_lines(spec.forbidden)

        # Surface-Pattern als ein gemeinsamer Spec für „liegt in irgendeinem
        # Surface". Pfade enden in der Spec oft auf `/` (Verzeichnis-Prefix);
        # wir hängen `**` an, damit alle Files darunter matchen.
        surface_lines: list[str] = []
        for surf in spec.surfaces.values():
            for p in surf.paths:
                if p.endswith("/"):
                    surface_lines.append(p + "**")
                surface_lines.append(p)
        self._surfaces = GitIgnoreSpec.from_lines(surface_lines)

        # Capability.run-Liste als pathspec, weil dieselbe Glob-Syntax greift
        self._run_allowlist = GitIgnoreSpec.from_lines(spec.capabilities.run)

    # --- Pfad-Aktionen --------------------------------------------------

    def check_edit(self, path: str) -> CheckResult:
        """Prüft, ob ein Pfad editiert werden darf.

        Reihenfolge: forbidden zuerst (überstimmt alles), dann muss der Pfad
        in einer Surface liegen.
        """
        norm = path.replace("\\", "/").lstrip("./")

        if self._forbidden.match_file(norm):
            return CheckResult(
                allowed=False,
                guardrail_id="forbidden_path",
                detail=f"path {path!r} matches forbidden pattern",
            )

        if not self._surfaces.match_file(norm):
            return CheckResult(
                allowed=False,
                guardrail_id="surface_violation",
                detail=f"path {path!r} is not in any declared surface",
            )

        return _OK

    def check_read(self, path: str) -> CheckResult:
        """Lesen ist breiter als Editieren — nur forbidden blockt.

        `capabilities.read` enthält in der Default-Spec `**/*`, also alles.
        Forbidden-Pfade sind aber auch read-blocked, damit Prompts keine
        sensitiven Files (auth.py, payments.py) als Kontext bekommen.
        """
        norm = path.replace("\\", "/").lstrip("./")
        if self._forbidden.match_file(norm):
            return CheckResult(
                allowed=False,
                guardrail_id="forbidden_path",
                detail=f"read of forbidden path {path!r}",
            )
        return _OK

    # --- Befehls-Aktionen ----------------------------------------------

    def check_run(self, command: str) -> CheckResult:
        """Prüft, ob ein Shell-Command ausgeführt werden darf.

        Vergleicht den Command-String gegen `capabilities.run`-Liste, gitignore-
        Stil. `pytest *` matcht `pytest -x -q tests/`. Default-deny: alles, was
        nicht explizit erlaubt ist, wird abgelehnt.
        """
        # Match path-style — pathspec arbeitet auf "Pfad"-Strings, aber das
        # passt für unsere flachen Command-Strings genauso.
        if self._run_allowlist.match_file(command):
            return _OK
        return CheckResult(
            allowed=False,
            guardrail_id="capability_denied",
            detail=f"command {command!r} not in capabilities.run allowlist",
        )

    # --- Boolean-Aktionen ----------------------------------------------

    def check_action(self, action: CapabilityAction) -> CheckResult:
        """Prüft eine boolesche Capability."""
        # Hart deaktivierte Aktionen IMMER ablehnen — auch wenn die Spec
        # fehlerhaft true sagt (sollte vom Spec-Loader nicht durchkommen,
        # aber Defense-in-depth). `merge_pr` steht hier NICHT mehr: es ist
        # seit dem Agent-Review-Merge opt-in und folgt der Spec-Flag (unten).
        if action in ("push_to_main", "push_force"):
            return CheckResult(
                allowed=False,
                guardrail_id="capability_denied",
                detail=f"{action} is categorically disabled in v1",
            )

        flag = getattr(self.spec.capabilities, action, None)
        if flag is True:
            return _OK
        return CheckResult(
            allowed=False,
            guardrail_id="capability_denied",
            detail=f"capability {action!r} is not enabled in spec",
        )

    # --- Egress --------------------------------------------------------

    def check_egress(self, host: str) -> CheckResult:
        """Prüft, ob Netzwerkzugriff auf einen Host erlaubt ist."""
        allowlist = self.spec.capabilities.network_egress
        if host in allowlist:
            return _OK
        # Wildcard-Hosts wie `*.example.com` nicht in v1 — exakter Match.
        return CheckResult(
            allowed=False,
            guardrail_id="egress_denied",
            detail=f"host {host!r} not in network_egress allowlist",
        )

    # --- Helpers für CLI-Übersetzung -----------------------------------

    def allowed_tools_string(self) -> str:
        """Übersetzt die Capabilities in einen `--allowedTools`-String für
        Claude Code CLI.

        Format: kommagetrennte Liste von Tool-Namen, Bash-Patterns in Klammern:
            "Read,Edit,Write,Bash(pytest:*),Bash(ruff:*)"

        Read und Edit sind immer erlaubt; Edit zusätzlich auf die Surfaces
        eingeschränkt — die Pfad-Begrenzung läuft aber nicht über --allowedTools,
        sondern über den Pre-/Post-Check im Capability-Enforcer und
        `.claude/hooks/PreToolUse`.
        """
        tools: list[str] = ["Read"]
        if self.spec.capabilities.edit:
            tools.extend(["Edit", "Write"])
        for cmd_pattern in self.spec.capabilities.run:
            # "pytest *" → "Bash(pytest:*)"
            tool_part = _command_pattern_to_bash_tool(cmd_pattern)
            if tool_part:
                tools.append(tool_part)
        return ",".join(tools)


def _command_pattern_to_bash_tool(pattern: str) -> str | None:
    """Übersetzt ein Run-Pattern aus der Spec in das Bash-Tool-Format.

        "pytest *"     → "Bash(pytest:*)"
        "npm test"     → "Bash(npm test:*)"
        "npm run lint*" → "Bash(npm run lint*:*)"
    """
    pattern = pattern.strip()
    if not pattern:
        return None
    # Erstes Token = Programm
    parts = pattern.split(None, 1)
    if len(parts) == 1:
        head = parts[0]
        rest = ""
    else:
        head, rest = parts
    if rest in ("", "*"):
        return f"Bash({head}:*)"
    return f"Bash({head} {rest}:*)"
