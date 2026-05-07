"""Tests für forge_execute.worktrees.

Brauchen ein echtes Git, weil wir gegen `git worktree` arbeiten.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from forge_execute.worktrees import (
    GitError,
    PatchApplyError,
    WorktreeManager,
)


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=check,
        encoding="utf-8",
        errors="replace",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Initialisiert ein leeres Git-Repo mit einem Initial-Commit."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@forge.local")
    _git(repo_root, "config", "user.name", "forge-test")
    (repo_root / "README.md").write_text("# test\n", encoding="utf-8")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n",
        encoding="utf-8",
    )
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "initial")
    return repo_root


def test_create_and_cleanup(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="01HZX001")
    assert wt.path.is_dir()
    assert wt.branch == "forge/01HZX001"
    assert (wt.path / "src" / "calc.py").exists()
    wm.cleanup(wt)
    assert not wt.path.exists()


def test_three_parallel_worktrees(repo: Path) -> None:
    wm = WorktreeManager(repo)
    worktrees = [wm.create(run_id=f"r{i}") for i in range(3)]
    try:
        for wt in worktrees:
            assert wt.path.is_dir()
            assert (wt.path / "src" / "calc.py").exists()
        assert len({wt.path for wt in worktrees}) == 3
    finally:
        for wt in worktrees:
            wm.cleanup(wt)


def test_apply_patch_modifies_file(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        diff = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    return a + b\n"
        )
        wm.apply_patch(wt, diff)
        content = (wt.path / "src" / "calc.py").read_text(encoding="utf-8")
        assert "return a + b" in content
        assert "return a - b" not in content
    finally:
        wm.cleanup(wt)


def test_apply_patch_idempotent_with_revert(repo: Path) -> None:
    """apply + revert == identity."""
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        original = (wt.path / "src" / "calc.py").read_text(encoding="utf-8")
        diff = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def add(a, b):\n"
            "-    return a - b\n"
            "+    return a + b\n"
        )
        wm.apply_patch(wt, diff)
        assert (wt.path / "src" / "calc.py").read_text(encoding="utf-8") != original
        wm.revert(wt)
        assert (wt.path / "src" / "calc.py").read_text(encoding="utf-8") == original
    finally:
        wm.cleanup(wt)


def test_apply_patch_rejects_bad_diff(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        bad_diff = (
            "diff --git a/src/calc.py b/src/calc.py\n"
            "--- a/src/calc.py\n"
            "+++ b/src/calc.py\n"
            "@@ -1,2 +1,2 @@\n"
            " WRONG_CONTEXT_LINE\n"
            "-    return a - b\n"
            "+    return a + b\n"
        )
        with pytest.raises(PatchApplyError):
            wm.apply_patch(wt, bad_diff)
    finally:
        wm.cleanup(wt)


def test_commit_returns_sha_and_advances_head(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        sha = wm.commit(wt, "forge: test commit")
        assert len(sha) >= 7
        assert sha != wt.base_commit
    finally:
        wm.cleanup(wt)


def test_diff_against_base(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        wm.commit(wt, "forge: change behavior")
        diff = wm.diff_against_base(wt)
        assert "-    return a - b" in diff
        assert "+    return a + b" in diff
    finally:
        wm.cleanup(wt)


def test_changed_files_lists_modifications(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        wm.commit(wt, "forge: change")
        files = wm.changed_files(wt)
        assert "src/calc.py" in files
    finally:
        wm.cleanup(wt)


def test_has_changes_false_on_clean_worktree(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        assert wm.has_changes(wt) is False
    finally:
        wm.cleanup(wt)


def test_create_rejects_existing_path(repo: Path) -> None:
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        with pytest.raises(GitError):
            wm.create(run_id="r1")
    finally:
        wm.cleanup(wt)


def test_revert_with_explicit_to_commit(repo: Path) -> None:
    """Revert kann auf einen anderen Commit als base zurücksetzen — wichtig
    für KEEP→DISCARD-Sequenz, damit DISCARD nicht KEPT-commits wegblasten.
    """
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        # Erste Mutation: commit
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        sha_after_keep = wm.commit(wt, "forge: keep")

        # Zweite Mutation: ändert was, dann discard
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a * b  # buggy\n", encoding="utf-8"
        )
        # revert auf sha_after_keep — die zweite Mutation wird verworfen,
        # die erste KEPT-commit bleibt erhalten
        wm.revert(wt, to_commit=sha_after_keep)

        content = (wt.path / "src" / "calc.py").read_text(encoding="utf-8")
        assert content == "def add(a, b):\n    return a + b\n", (
            "DISCARD nach KEEP hat die KEPT-Änderung weggeblasten"
        )
    finally:
        wm.cleanup(wt)
