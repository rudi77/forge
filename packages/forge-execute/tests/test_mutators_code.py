"""Tests für forge_execute.mutators.code."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge_core.spec import ProjectSpec
from forge_execute.capabilities import Capabilities
from forge_execute.mutators.code import (
    CodeMutator,
    count_diff_lines,
    extract_changed_paths,
)
from forge_execute.worktrees import WorktreeManager


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def _spec_with_surface(path_prefix: str) -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "test",
            "cost_caps": {
                "per_generation_usd": "0.5",
                "per_run_usd": "5",
                "per_project_per_day_usd": "30",
                "per_project_per_month_usd": "500",
            },
            "surfaces": {
                "code": {"paths": [path_prefix], "type": "code"},
            },
            "forbidden": ["src/forbidden.py"],
        }
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "test@forge.local")
    _git(repo_root, "config", "user.name", "forge-test")
    (repo_root / "src").mkdir()
    (repo_root / "src" / "calc.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8"
    )
    (repo_root / "src" / "forbidden.py").write_text(
        "SECRET = 'do not touch'\n", encoding="utf-8"
    )
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "initial")
    return repo_root


@pytest.fixture
def context(repo: Path):
    spec = _spec_with_surface("src/")
    caps = Capabilities(spec)
    wm = WorktreeManager(repo)
    wt = wm.create(run_id="r1")
    yield wm, caps, wt
    wm.cleanup(wt)


# --- Diff-Parser ---------------------------------------------------------


def test_extract_paths_from_git_diff_header() -> None:
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    assert extract_changed_paths(diff) == ["src/calc.py"]


def test_extract_paths_multiple_files() -> None:
    diff = (
        "diff --git a/a.py b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.py b/b.py\n@@ -1 +1 @@\n-x\n+y\n"
    )
    assert extract_changed_paths(diff) == ["a.py", "b.py"]


def test_extract_paths_fallback_plus_header() -> None:
    diff = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-x\n+y\n"
    assert extract_changed_paths(diff) == ["foo.py"]


def test_count_diff_lines() -> None:
    diff = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1,3 +1,4 @@\n"
        " ctx\n"
        "-old1\n"
        "-old2\n"
        "+new1\n"
        "+new2\n"
        "+new3\n"
    )
    added, removed = count_diff_lines(diff)
    assert added == 3
    assert removed == 2


# --- Mutator end-to-end --------------------------------------------------


def test_apply_valid_diff_succeeds(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a + b\n"
    )
    result = mutator.apply(wt, diff)
    assert result.success is True
    assert result.files_changed == ["src/calc.py"]
    assert result.lines_added == 1
    assert result.lines_removed == 1


def test_apply_blocks_forbidden_path(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    diff = (
        "diff --git a/src/forbidden.py b/src/forbidden.py\n"
        "--- a/src/forbidden.py\n"
        "+++ b/src/forbidden.py\n"
        "@@ -1 +1 @@\n"
        "-SECRET = 'do not touch'\n"
        "+SECRET = 'leaked'\n"
    )
    result = mutator.apply(wt, diff)
    assert result.success is False
    assert result.error_class == "CapabilityDenied"
    assert result.capability_violation is not None
    assert result.capability_violation.guardrail_id == "forbidden_path"


def test_apply_blocks_path_outside_surfaces(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    # Path außerhalb von src/
    diff = (
        "diff --git a/elsewhere.py b/elsewhere.py\n"
        "--- /dev/null\n"
        "+++ b/elsewhere.py\n"
        "@@ -0,0 +1 @@\n"
        "+x = 1\n"
    )
    result = mutator.apply(wt, diff)
    assert result.success is False
    assert result.error_class == "CapabilityDenied"


def test_apply_rejects_diff_introducing_syntax_error(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def add(a, b):\n"
        "-    return a - b\n"
        "+    return a +\n"  # syntax error
    )
    result = mutator.apply(wt, diff)
    assert result.success is False
    assert result.error_class == "SyntaxError"
    # Worktree wurde reverted
    content = (wt.path / "src" / "calc.py").read_text(encoding="utf-8")
    assert content == "def add(a, b):\n    return a - b\n"


def test_apply_rejects_malformed_diff(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    diff = (
        "diff --git a/src/calc.py b/src/calc.py\n"
        "--- a/src/calc.py\n"
        "+++ b/src/calc.py\n"
        "@@ -1,2 +1,2 @@\n"
        " WRONG_CONTEXT\n"
        "-something\n"
        "+something else\n"
    )
    result = mutator.apply(wt, diff)
    assert result.success is False
    assert result.error_class == "PatchApplyError"


def test_apply_empty_diff_fails(context) -> None:
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
    result = mutator.apply(wt, "")
    assert result.success is False
    assert result.error_class == "EmptyDiff"


def test_apply_then_revert_roundtrip(context) -> None:
    """apply + revert == identity (Spec Teil 6.2)."""
    wm, caps, wt = context
    mutator = CodeMutator(worktrees=wm, capabilities=caps)
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
    result = mutator.apply(wt, diff)
    assert result.success is True
    wm.revert(wt)
    after_revert = (wt.path / "src" / "calc.py").read_text(encoding="utf-8")
    assert after_revert == original
