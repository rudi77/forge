"""Code-Mutator.

Nimmt einen Unified Diff, validiert ihn gegen Surfaces+Forbidden+Capabilities,
appliziert ihn auf einen Worktree, und prüft Syntax der geänderten Python-Files.

Aus Spec Teil 6.2: vor jedem Apply
    1. Pfad-Check gegen `surfaces` und `forbidden`
    2. Capability-Check gegen `capabilities.edit`
    3. Mutator-spezifische Validierung (z.B. AST-parse)

Nach erfolgreichem Apply: `git diff --check` (Whitespace), Syntax-Check
für relevante Sprachen (Python via py_compile; tsc/eslint sind dem Preflight
überlassen).
"""

from __future__ import annotations

import ast
import re
import subprocess
from dataclasses import dataclass, field

from forge_execute.capabilities import Capabilities, CheckResult
from forge_execute.worktrees import PatchApplyError, Worktree, WorktreeManager

# Erkennt die Header-Zeile eines unified diff-Hunks für eine Datei:
#     diff --git a/src/foo.py b/src/foo.py
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(?P<a>.+?) b/(?P<b>.+)$", re.MULTILINE)
# Fallback: --- a/<path>  bzw. +++ b/<path>
_PLUS_HEADER_RE = re.compile(r"^\+\+\+ b/(?P<path>.+)$", re.MULTILINE)


@dataclass(frozen=True)
class MutationResult:
    """Ergebnis eines Mutator-Aufrufs.

    `success=False` heißt: nichts wurde verändert (entweder vor dem Apply
    abgebrochen oder Revert nach Syntax-Failure).
    """

    success: bool
    mutator: str = "code"
    files_changed: list[str] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    error_class: str | None = None
    error_msg: str | None = None
    capability_violation: CheckResult | None = None
    """Falls eine Capability-Verletzung den Apply gestoppt hat."""


class CodeMutator:
    """Wendet Unified Diffs an, mit Capability-Pre-Check und Syntax-Post-Check.

    Verwendung::

        mutator = CodeMutator(worktrees=wm, capabilities=caps)
        result = mutator.apply(worktree, diff)
        if not result.success:
            # GuardrailViolation oder PreflightFailed je nach error_class
            ...
    """

    def __init__(
        self,
        *,
        worktrees: WorktreeManager,
        capabilities: Capabilities,
    ) -> None:
        self.worktrees = worktrees
        self.capabilities = capabilities

    def apply(self, worktree: Worktree, diff: str) -> MutationResult:
        # 1. Pfad-Extraktion + Capability-Check
        target_paths = extract_changed_paths(diff)
        if not target_paths:
            return MutationResult(
                success=False,
                error_class="EmptyDiff",
                error_msg="diff did not reference any files",
            )

        for path in target_paths:
            check = self.capabilities.check_edit(path)
            if check.denied:
                return MutationResult(
                    success=False,
                    error_class="CapabilityDenied",
                    error_msg=f"path {path!r} not editable: {check.detail}",
                    capability_violation=check,
                )

        # 2. Apply
        try:
            self.worktrees.apply_patch(worktree, diff)
        except PatchApplyError as exc:
            return MutationResult(
                success=False,
                error_class="PatchApplyError",
                error_msg=str(exc),
            )

        # 3. Whitespace-Check + Syntax-Check
        ws_err = self._whitespace_check(worktree)
        if ws_err is not None:
            self.worktrees.revert(worktree)
            return MutationResult(
                success=False,
                error_class="WhitespaceError",
                error_msg=ws_err,
            )

        syntax_err = self._syntax_check(worktree, target_paths)
        if syntax_err is not None:
            self.worktrees.revert(worktree)
            return MutationResult(
                success=False,
                error_class="SyntaxError",
                error_msg=syntax_err,
            )

        # 4. Stats
        added, removed = count_diff_lines(diff)
        return MutationResult(
            success=True,
            files_changed=target_paths,
            lines_added=added,
            lines_removed=removed,
        )

    # --- Internal checks ----------------------------------------------

    @staticmethod
    def _whitespace_check(worktree: Worktree) -> str | None:
        """`git diff --check` meldet Whitespace-Errors. Der Diff-Vergleich ist
        gegen den Index, nach apply liegen Änderungen aber noch im Working
        Tree — also mit `--staged`-Pendant `git diff --check HEAD`."""
        result = subprocess.run(
            ["git", "diff", "--check", "HEAD"],
            cwd=str(worktree.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        # `git diff --check` Exit 0 = clean, 1 = Whitespace-Errors gefunden
        if result.returncode == 0:
            return None
        return result.stdout.strip() or result.stderr.strip()

    @staticmethod
    def _syntax_check(worktree: Worktree, paths: list[str]) -> str | None:
        """Parst geänderte Python-Files mit `ast.parse`.

        AST-Parse läuft im aktuellen Python-Prozess — schneller als
        `py_compile` als Subprozess und genauso aussagekräftig.
        """
        for rel in paths:
            if not rel.endswith(".py"):
                continue
            full = worktree.path / rel
            if not full.is_file():
                # Datei wurde durch den Diff entfernt — kein Syntax-Check nötig
                continue
            try:
                source = full.read_text(encoding="utf-8")
                ast.parse(source, filename=str(full))
            except SyntaxError as exc:
                return f"{rel}: {exc.msg} at line {exc.lineno}"
            except UnicodeDecodeError as exc:
                return f"{rel}: not valid UTF-8: {exc}"
        return None


# --- Diff-Parser-Helpers ----------------------------------------------


def extract_changed_paths(diff: str) -> list[str]:
    """Extrahiert die Liste betroffener Dateipfade aus einem unified Diff.

    Sucht zuerst `diff --git a/X b/X`-Header (Standard `git diff`-Format).
    Fallback: `+++ b/X`-Zeilen. Pfade werden in Reihenfolge des Auftretens
    deduped zurückgegeben.
    """
    paths: list[str] = []
    seen: set[str] = set()

    for m in _DIFF_HEADER_RE.finditer(diff):
        path = m.group("b")
        if path not in seen:
            seen.add(path)
            paths.append(path)

    if not paths:
        for m in _PLUS_HEADER_RE.finditer(diff):
            path = m.group("path")
            if path == "/dev/null":
                continue
            if path not in seen:
                seen.add(path)
                paths.append(path)

    return paths


def count_diff_lines(diff: str) -> tuple[int, int]:
    """Zählt hinzugefügte (+) und entfernte (-) Zeilen, ignoriert Header."""
    added = 0
    removed = 0
    for line in diff.splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            continue
        if line.startswith("+"):
            added += 1
        elif line.startswith("-"):
            removed += 1
    return added, removed
