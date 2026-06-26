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


def test_commit_returns_head_when_agent_already_committed(repo: Path) -> None:
    """Agent hat seine Arbeit SELBST committet (sauberer Working Tree).

    Beobachtet bei einem resumeten Run: claude committete die WP7-Subtasks
    selbst, dann scheiterte der Runner-KEEP-Commit an `git commit` (nothing to
    commit, returncode≠0, stderr leer). commit() muss in diesem Fall den
    aktuellen HEAD zurückgeben — er trägt die Änderungen bereits.
    """
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="01HZXAGENTCOMMIT")
    (wt.path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    _git(wt.path, "add", "-A")
    _git(wt.path, "commit", "-m", "forge: WIP | agent self-commit")
    head = wm._rev_parse("HEAD", cwd=wt.path)

    # Runner-KEEP-Pfad mit den (bereits committeten) Files → kein Raise, HEAD.
    sha = wm.commit(wt, "forge: keep | gen 0", paths=["src/calc.py"])
    assert sha == head
    wm.cleanup(wt)


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


def test_changed_files_lists_untracked_new_files(repo: Path) -> None:
    """Neu angelegte (untracked) Files müssen erscheinen — der Greenfield-Bug.

    Der Coding-Agent editiert den Worktree direkt und committet oft nicht;
    neue Dateien sind dann untracked. `git diff base HEAD` sieht sie nicht —
    `changed_files` muss sie trotzdem melden, sonst umgehen sie Capability-
    Check und KEEP-Commit.
    """
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        # Eine neue Datei anlegen, NICHT committen.
        (wt.path / "src" / "new_module.py").write_text(
            "def f():\n    return 1\n", encoding="utf-8"
        )
        # Eine bestehende Datei ändern, NICHT committen.
        (wt.path / "src" / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8"
        )
        files = wm.changed_files(wt)
        assert "src/new_module.py" in files  # untracked
        assert "src/calc.py" in files  # uncommitted tracked
    finally:
        wm.cleanup(wt)


def test_changed_files_excludes_claude_subagents(repo: Path) -> None:
    """Transiente `.claude/`-Subagent-Markdowns dürfen nie auftauchen —
    sonst landen sie im Capability-Check und im PR-Commit."""
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        (wt.path / ".claude" / "agents").mkdir(parents=True)
        (wt.path / ".claude" / "agents" / "architect.md").write_text(
            "transient", encoding="utf-8"
        )
        (wt.path / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
        files = wm.changed_files(wt)
        assert "src/real.py" in files
        assert not any(f.startswith(".claude/") for f in files)
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


def test_list_forge_worktrees_filters_prefix(repo: Path) -> None:
    """Nur forge/* Worktrees zählen — fremde Branches ignoriert."""
    wm = WorktreeManager(repo)
    # Operator-eigener Worktree (kein forge/-Prefix) — soll ignoriert werden.
    other_path = repo.parent / "other"
    _git(repo, "worktree", "add", "-b", "feature/foo", str(other_path))
    try:
        wt = wm.create(run_id="r1")
        try:
            entries = wm.list_forge_worktrees()
            branches = {e["branch"] for e in entries}
            assert "forge/r1" in branches
            assert "feature/foo" not in branches
        finally:
            wm.cleanup(wt)
    finally:
        _git(repo, "worktree", "remove", "--force", str(other_path), check=False)


def test_gc_stale_removes_dangling_directory(repo: Path) -> None:
    """Worktree-Verzeichnis manuell weglöschen → gc_stale räumt den Eintrag."""
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    # Verzeichnis hart entfernen, ohne git Bescheid zu sagen → prunable.
    import shutil as _shutil
    _shutil.rmtree(wt.path)
    removed = wm.gc_stale()
    assert any("r1" in p for p in removed)
    # Eintrag ist weg.
    entries = wm.list_forge_worktrees()
    assert not any("r1" in str(e.get("branch", "")) for e in entries)


def test_gc_stale_removes_orphaned_pool_directory(repo: Path) -> None:
    """Verzeichnis existiert + Eintrag existiert, aber kein Run aktiv → entfernt."""
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    assert wt.path.is_dir()
    # Wir simulieren "Crash mid-run": keinen cleanup() aufrufen, gc_stale räumt.
    removed = wm.gc_stale()
    assert any("r1" in p for p in removed)
    assert not wt.path.exists()


def test_gc_stale_leaves_external_worktrees_alone(repo: Path) -> None:
    """forge/* Worktrees außerhalb des Pools werden nicht angefasst —
    falls ein Operator manuell einen forge-Branch in seinem eigenen
    Verzeichnis auscheckt."""
    wm = WorktreeManager(repo)
    external = repo.parent / "external-forge"
    _git(repo, "worktree", "add", "-b", "forge/manual", str(external))
    try:
        removed = wm.gc_stale()
        assert not any("external-forge" in p for p in removed)
        assert external.exists()
    finally:
        _git(repo, "worktree", "remove", "--force", str(external), check=False)
        _git(repo, "branch", "-D", "forge/manual", check=False)


def test_prune_merged_branches_skips_active_branch(repo: Path) -> None:
    """Branch mit aktivem Worktree wird übersprungen, auch wenn upstream gone."""
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    try:
        # Kein Remote vorhanden → upstream ist auch nicht "gone" sondern leer
        # → Branch wird übersprungen. Wichtig: kein Crash.
        deleted = wm.prune_merged_branches()
        assert "forge/r1" not in deleted
    finally:
        wm.cleanup(wt)


def test_prune_merged_branches_deletes_gone_upstream(repo: Path, tmp_path: Path) -> None:
    """Wenn der Branch ein Remote-Tracking hat das auf [gone] steht, wird er gelöscht."""
    # Bare-Repo als Remote.
    remote = tmp_path / "remote.git"
    _git(repo, "init", "--bare", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))

    wm = WorktreeManager(repo)
    wt = wm.create(run_id="rgone")
    # Wenigstens einen Commit im Worktree, damit push klappt.
    (wt.path / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    sha = wm.commit(wt, "forge: stub")
    assert sha
    _git(wt.path, "push", "-u", "origin", "forge/rgone")
    # Worktree weg, damit der Branch nicht mehr "checked out" ist.
    wm.cleanup(wt)
    # Remote-Branch hart entfernen → tracking wird auf [gone] gesetzt
    # nachdem fetch --prune lief.
    _git(repo, "push", "origin", "--delete", "forge/rgone")

    deleted = wm.prune_merged_branches()
    assert "forge/rgone" in deleted


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


def test_revert_preserves_venv(repo: Path) -> None:
    """revert() löscht das Eval-erzeugte `.venv` NICHT.

    Das venv ist ein regenerierbares Tooling-Artefakt; auf Windows lockt der
    laufende Python-Prozess die geladenen nativen Libs, sodass `git clean -fdx`
    daran scheiterte und den ganzen Run crashte. Untracked *Quellen* müssen
    weiterhin entfernt werden.
    """
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="rvenv")
    try:
        (wt.path / "src" / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        venv_lib = wt.path / ".venv" / "Lib"
        venv_lib.mkdir(parents=True)
        (venv_lib / "marker.txt").write_text("native lib", encoding="utf-8")

        wm.revert(wt)

        assert not (wt.path / "src" / "new_file.py").exists(), (
            "untracked Quelle muss vom revert entfernt werden"
        )
        assert (venv_lib / "marker.txt").exists(), (
            ".venv darf vom revert NICHT entfernt werden"
        )
    finally:
        wm.cleanup(wt)


def test_revert_tolerates_locked_files(repo: Path, monkeypatch) -> None:
    """Un-löschbare (gesperrte) Dateien beim clean crashen den Run nicht.

    `git reset --hard` hat den Quellstand bereits hergestellt; reine
    "failed to remove"-Warnungen von `git clean` werden toleriert.
    """
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="rlock")
    try:
        real_run = WorktreeManager._run

        def fake_run(cmd, *, cwd, stdin=None, check=False):
            if cmd[:2] == ["git", "clean"]:
                return subprocess.CompletedProcess(
                    cmd,
                    1,
                    stdout="",
                    stderr="warning: failed to remove .venv/x.pyd: Invalid argument",
                )
            return real_run(cmd, cwd=cwd, stdin=stdin, check=check)

        monkeypatch.setattr(WorktreeManager, "_run", staticmethod(fake_run))
        # Darf NICHT raisen.
        wm.revert(wt)
    finally:
        monkeypatch.undo()
        wm.cleanup(wt)
