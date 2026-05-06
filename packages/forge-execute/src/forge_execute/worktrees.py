"""Git-Worktree-Pool.

Jeder Run bekommt seinen eigenen Worktree, sauberes Idempotenz-Verhalten:
apply + revert == identity (Spec Teil 6.2).

Layout: `.forge/worktrees/<run_id>/` — gitignored. Pro Worktree eine eigene
venv kann später dazukommen (Optimierung); v1 reicht ein Worktree pro Run.

API laut todos.txt Schritt 3a:

    create(run_id) -> Path
    cleanup(path)
    apply_patch(path, diff)
    revert(path)
    commit(path, message)
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    """Generischer Git-Fehler — Caller sollte das in `GuardrailViolation` oder
    `error_class` ummappen."""


class PatchApplyError(GitError):
    """`git apply` ist fehlgeschlagen — der Diff passt nicht auf den Worktree."""


@dataclass(frozen=True)
class Worktree:
    """Handle auf einen aktiven Worktree mit dem zugehörigen Branch."""

    path: Path
    branch: str
    base_commit: str
    """Commit-SHA, von dem der Worktree gestartet ist — Anker für Revert."""


class WorktreeManager:
    """Verwaltet Git-Worktrees unter `<repo>/.forge/worktrees/`.

    Verwendung::

        wm = WorktreeManager(repo_root=Path("/path/to/repo"))
        wt = wm.create(run_id="01HZX001", base_ref="main")
        wm.apply_patch(wt, diff)
        wm.commit(wt, "forge: legacy_test_revival | gen 1 | composite +0.04")
        wm.revert(wt)
        wm.cleanup(wt)
    """

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.worktree_root = self.repo_root / ".forge" / "worktrees"
        self.worktree_root.mkdir(parents=True, exist_ok=True)

    # --- Lifecycle ------------------------------------------------------

    def create(self, *, run_id: str, base_ref: str = "HEAD") -> Worktree:
        """Erzeugt einen neuen Worktree mit eigenem Branch.

        Branch-Name: `forge/<run_id>` — kollisionsfrei, weil run_ids ULIDs sind.
        Wenn der Branch existiert (Wiederaufnahme), wird er wiederverwendet.
        """
        path = self.worktree_root / run_id
        if path.exists():
            raise GitError(f"worktree path already exists: {path}")

        branch = f"forge/{run_id}"
        base_commit = self._rev_parse(base_ref)

        cmd = ["git", "worktree", "add", "-b", branch, str(path), base_commit]
        result = self._run(cmd, cwd=self.repo_root)
        if result.returncode != 0:
            raise GitError(
                f"git worktree add failed (exit {result.returncode}): {result.stderr.strip()}"
            )

        return Worktree(path=path, branch=branch, base_commit=base_commit)

    def cleanup(self, worktree: Worktree) -> None:
        """Entfernt den Worktree (verzeichnis + git-internen Eintrag).

        Der Branch bleibt bestehen — Caller entscheidet, ob er ihn löschen will
        (typisch für DISCARD-Runs nach dem Push, oder gar nicht für mergebare).
        """
        # `git worktree remove --force` räumt auch dann auf, wenn der Worktree
        # uncommittete Änderungen hat (z.B. mid-mutation ein Crash).
        result = self._run(
            ["git", "worktree", "remove", "--force", str(worktree.path)],
            cwd=self.repo_root,
        )
        if result.returncode != 0:
            # Fallback: Verzeichnis manuell entfernen + prune
            if worktree.path.exists():
                shutil.rmtree(worktree.path, ignore_errors=True)
            self._run(["git", "worktree", "prune"], cwd=self.repo_root)

    def cleanup_branch(self, worktree: Worktree) -> None:
        """Optional: löscht den Branch nach Cleanup."""
        self._run(
            ["git", "branch", "-D", worktree.branch],
            cwd=self.repo_root,
            check=False,
        )

    # --- Mutationen -----------------------------------------------------

    def apply_patch(self, worktree: Worktree, diff: str) -> None:
        """Wendet einen unified diff via `git apply` an.

        Vor dem Apply läuft `--check`, damit fehlerhafte Diffs nicht halb
        appliziert werden.
        """
        # --check zuerst
        check = self._run(
            ["git", "apply", "--check", "-"],
            cwd=worktree.path,
            stdin=diff,
        )
        if check.returncode != 0:
            raise PatchApplyError(
                f"git apply --check failed: {check.stderr.strip()}"
            )

        applied = self._run(
            ["git", "apply", "-"],
            cwd=worktree.path,
            stdin=diff,
        )
        if applied.returncode != 0:
            raise PatchApplyError(
                f"git apply failed unexpectedly after check passed: "
                f"{applied.stderr.strip()}"
            )

    def revert(self, worktree: Worktree) -> None:
        """Setzt den Worktree zurück auf seinen `base_commit`.

        Discard-Pfad: Nach einer DISCARD-Entscheidung will der Runner den
        Worktree „leerräumen", damit die nächste Generation auf einer sauberen
        Basis startet.
        """
        # Hard reset auf base_commit + clean (untracked files raus)
        reset = self._run(
            ["git", "reset", "--hard", worktree.base_commit],
            cwd=worktree.path,
        )
        if reset.returncode != 0:
            raise GitError(f"git reset --hard failed: {reset.stderr.strip()}")
        clean = self._run(["git", "clean", "-fdx"], cwd=worktree.path)
        if clean.returncode != 0:
            raise GitError(f"git clean failed: {clean.stderr.strip()}")

    def commit(
        self,
        worktree: Worktree,
        message: str,
        *,
        paths: list[str] | None = None,
        allow_empty: bool = False,
    ) -> str:
        """Stagest und committed Änderungen im Worktree.

        Args:
            paths: Wenn gegeben, werden nur diese Files gestaged. Sonst
                `git add -A` (alles). Pfade sind relativ zum Worktree.
                Caller, die Subagent-Files (z.B. `.claude/agents/*.md` von
                forge-execute) NICHT committen wollen, übergeben hier eine
                Whitelist.

        Returns:
            Commit-SHA des neuen Commits.
        """
        if paths:
            add = self._run(["git", "add", "--", *paths], cwd=worktree.path)
        else:
            add = self._run(["git", "add", "-A"], cwd=worktree.path)
        if add.returncode != 0:
            raise GitError(f"git add failed: {add.stderr.strip()}")

        cmd = ["git", "commit", "-m", message]
        if allow_empty:
            cmd.append("--allow-empty")
        result = self._run(cmd, cwd=worktree.path)
        if result.returncode != 0:
            raise GitError(f"git commit failed: {result.stderr.strip()}")

        return self._rev_parse("HEAD", cwd=worktree.path)

    # --- Inspection ----------------------------------------------------

    def diff_against_base(self, worktree: Worktree) -> str:
        """Liefert den Diff vom base_commit bis zum aktuellen HEAD im Worktree.

        Das ist der Diff, den der Coding-Agent produziert hat — Input für die
        PR-Erzeugung.
        """
        result = self._run(
            ["git", "diff", worktree.base_commit, "HEAD"],
            cwd=worktree.path,
        )
        if result.returncode != 0:
            raise GitError(f"git diff failed: {result.stderr.strip()}")
        return result.stdout

    def changed_files(self, worktree: Worktree) -> list[str]:
        """Liste der Files, die sich gegenüber base_commit geändert haben."""
        result = self._run(
            ["git", "diff", "--name-only", worktree.base_commit, "HEAD"],
            cwd=worktree.path,
        )
        if result.returncode != 0:
            raise GitError(f"git diff --name-only failed: {result.stderr.strip()}")
        return [line for line in result.stdout.splitlines() if line.strip()]

    def has_changes(self, worktree: Worktree) -> bool:
        """True, wenn der Worktree gegenüber base_commit modifiziert wurde
        (commited oder uncommited)."""
        return bool(self.changed_files(worktree)) or self._has_uncommitted(worktree)

    def _has_uncommitted(self, worktree: Worktree) -> bool:
        result = self._run(
            ["git", "status", "--porcelain"],
            cwd=worktree.path,
        )
        return bool(result.stdout.strip())

    # --- Helpers --------------------------------------------------------

    def _rev_parse(self, ref: str, *, cwd: Path | None = None) -> str:
        result = self._run(
            ["git", "rev-parse", ref],
            cwd=cwd or self.repo_root,
        )
        if result.returncode != 0:
            raise GitError(f"git rev-parse {ref} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    @staticmethod
    def _run(
        cmd: list[str],
        *,
        cwd: Path,
        stdin: str | None = None,
        check: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            cmd,
            cwd=str(cwd),
            input=stdin,
            capture_output=True,
            text=True,
            check=check,
            encoding="utf-8",
            errors="replace",
        )
