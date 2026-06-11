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

    # --- Garbage collection --------------------------------------------

    def list_forge_worktrees(self) -> list[dict[str, str | bool]]:
        """Liefert alle bei git registrierten Worktrees mit Branch-Prefix
        ``forge/`` (das sind unsere; alles andere ist Operator-eigen).

        Jeder Eintrag enthält ``path`` (str), ``branch`` (str, ohne
        ``refs/heads/``-Prefix) und ``prunable`` (bool — git markiert
        Worktrees als prunable, wenn das Verzeichnis fehlt o.ä.).
        """
        result = self._run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=self.repo_root,
        )
        if result.returncode != 0:
            raise GitError(
                f"git worktree list --porcelain failed: {result.stderr.strip()}"
            )

        entries: list[dict[str, str | bool]] = []
        current: dict[str, str | bool] = {}
        for line in result.stdout.splitlines():
            if not line.strip():
                if current:
                    self._maybe_record_forge_worktree(current, entries)
                    current = {}
                continue
            if line.startswith("worktree "):
                current["path"] = line[len("worktree ") :].strip()
            elif line.startswith("branch "):
                ref = line[len("branch ") :].strip()
                current["branch"] = ref.removeprefix("refs/heads/")
            elif line.startswith("prunable"):
                current["prunable"] = True
        if current:
            self._maybe_record_forge_worktree(current, entries)
        return entries

    @staticmethod
    def _maybe_record_forge_worktree(
        entry: dict[str, str | bool],
        out: list[dict[str, str | bool]],
    ) -> None:
        branch = entry.get("branch", "")
        if isinstance(branch, str) and branch.startswith("forge/"):
            entry.setdefault("prunable", False)
            out.append(entry)

    def gc_stale(self) -> list[str]:
        """Räumt verwaiste ``forge/*``-Worktrees auf.

        Zwei Klassen werden entfernt:

        * **Prunable**: git selbst hat sie als kaputt markiert (Verzeichnis
          weg, aber Eintrag in ``.git/worktrees/`` noch da). ``git worktree
          prune`` fasst die zusammen.
        * **Verzeichnis existiert noch**, aber kein Run hält das Lock —
          typisch nach einem Crash mitten in einer Iteration. Wir entfernen
          per ``git worktree remove --force``.

        Branches werden hier **nicht** gelöscht; das macht
        :meth:`prune_merged_branches` getrennt, weil dort die GitHub-
        Sicht über Merge-Status reinkommt.

        Returns: Liste der entfernten Worktree-Pfade (für Logging).
        """
        removed: list[str] = []
        for entry in self.list_forge_worktrees():
            path_str = str(entry.get("path", ""))
            if not path_str:
                continue
            path = Path(path_str)
            try:
                inside_pool = path.resolve().is_relative_to(self.worktree_root)
            except (OSError, ValueError):
                inside_pool = False
            # Nur unsere Pool-Worktrees anfassen — falls ein Operator
            # manuell einen forge/* Branch in einem fremden Worktree
            # auscheckt, fummeln wir da nicht rein.
            if not inside_pool:
                continue
            prunable = bool(entry.get("prunable", False))
            if prunable or not path.exists():
                # Eintrag verwaist — `git worktree prune` räumt den
                # gesamten Stapel weg.
                self._run(["git", "worktree", "prune"], cwd=self.repo_root)
                removed.append(path_str)
                continue
            # Verzeichnis existiert, aber kein aktiver Run greift —
            # forciert entfernen.
            result = self._run(
                ["git", "worktree", "remove", "--force", path_str],
                cwd=self.repo_root,
            )
            if result.returncode == 0:
                removed.append(path_str)
                continue
            # Fallback: rmtree + prune. Selbst wenn rmtree teilweise
            # fehlschlägt (Windows-File-Lock), markiert prune den
            # git-internen Eintrag als entfernbar.
            shutil.rmtree(path, ignore_errors=True)
            self._run(["git", "worktree", "prune"], cwd=self.repo_root)
            removed.append(path_str)
        return removed

    def prune_merged_branches(self) -> list[str]:
        """Löscht lokale ``forge/*``-Branches ohne Remote-Tracking.

        Hintergrund: ``gh pr merge --auto --delete-branch`` löscht
        beim Auto-Merge den Remote-Branch. ``git fetch --prune``
        entfernt anschließend den lokalen Tracking-Ref unter
        ``refs/remotes/origin/forge/<id>``. Was bleibt, ist der lokale
        ``refs/heads/forge/<id>`` — der zeigt jetzt ins Leere und
        wir können ihn lokal verwerfen.

        Diese Methode ruft erst ``git fetch --prune origin`` (best-
        effort; ignoriert Fehler, falls offline) und löscht dann alle
        ``forge/*``-Branches, deren upstream ``[gone]`` ist. Branches
        mit aktivem Worktree werden übersprungen — git verweigert sie
        sowieso, aber wir wollen die Fehlermeldung gar nicht erst.

        Returns: Liste der gelöschten Branch-Namen.
        """
        # Best-effort: prune Tracking-Refs.
        self._run(
            ["git", "fetch", "--prune", "origin"],
            cwd=self.repo_root,
            check=False,
        )

        # `git for-each-ref` mit upstream:track liefert "[gone]" für
        # Branches, deren Remote-Counterpart gelöscht wurde.
        result = self._run(
            [
                "git",
                "for-each-ref",
                "--format=%(refname:short)%09%(upstream:track)",
                "refs/heads/forge/",
            ],
            cwd=self.repo_root,
        )
        if result.returncode != 0:
            return []

        active_branches = {
            str(entry.get("branch", ""))
            for entry in self.list_forge_worktrees()
        }

        deleted: list[str] = []
        for line in result.stdout.splitlines():
            if "\t" not in line:
                continue
            branch, track = line.split("\t", 1)
            if not branch.startswith("forge/"):
                continue
            if branch in active_branches:
                continue
            if "[gone]" not in track:
                continue
            delete = self._run(
                ["git", "branch", "-D", branch],
                cwd=self.repo_root,
                check=False,
            )
            if delete.returncode == 0:
                deleted.append(branch)
        return deleted

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

    def revert(self, worktree: Worktree, *, to_commit: str | None = None) -> None:
        """Setzt den Worktree auf einen Commit zurück.

        Default ist `worktree.base_commit` (der Stand bei Run-Start). Der
        Runner overrided das nach einer KEEP-Generation auf den damaligen
        HEAD-SHA — sonst würde ein DISCARD in einer späteren Generation
        auch die schon committeten Änderungen aus der KEEP-Generation
        wegblasen.
        """
        target = to_commit or worktree.base_commit
        # Hard reset + clean (untracked files raus)
        reset = self._run(
            ["git", "reset", "--hard", target],
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
        """Alle Files, die sich gegenüber base_commit geändert haben.

        Erfasst drei Klassen in einem: **committed** Änderungen, **uncommitted**
        (tracked) Änderungen und **neu angelegte** (untracked) Files. Letztere
        fehlten früher (`git diff base HEAD` sieht weder uncommitted noch
        untracked) — die Folge war, dass vom Coding-Agent neu erstellte Dateien
        weder den Capability-Check durchliefen noch in den KEEP-Commit gelangten.

        Die transienten Subagent-Markdowns unter `.claude/` (von
        ``_install_subagents`` in den Worktree kopiert) werden ausgeschlossen —
        sie sind forge-intern und gehören nie in Diff, Capability-Check oder PR.
        """
        # `git diff --name-only <base>` (ohne zweite Ref) vergleicht den
        # Working Tree gegen base_commit — deckt committed + uncommitted ab.
        tracked = self._run(
            ["git", "diff", "--name-only", worktree.base_commit],
            cwd=worktree.path,
        )
        if tracked.returncode != 0:
            raise GitError(f"git diff --name-only failed: {tracked.stderr.strip()}")
        # Untracked Files separat — sie tauchen in keinem `git diff` auf.
        untracked = self._run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=worktree.path,
        )
        if untracked.returncode != 0:
            raise GitError(f"git ls-files --others failed: {untracked.stderr.strip()}")

        seen: set[str] = set()
        files: list[str] = []
        for line in (*tracked.stdout.splitlines(), *untracked.stdout.splitlines()):
            path = line.strip()
            if not path or path.startswith(".claude/") or path in seen:
                continue
            seen.add(path)
            files.append(path)
        return files

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
