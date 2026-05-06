"""Tests für forge_execute._venv."""

from __future__ import annotations

import os
import platform
from pathlib import Path

from forge_execute._venv import find_venv_for, venv_aware_env

_IS_WINDOWS = platform.system() == "Windows"
_BIN = "Scripts" if _IS_WINDOWS else "bin"
_PY = "python.exe" if _IS_WINDOWS else "python"


def _make_fake_venv(parent: Path, name: str = ".venv") -> Path:
    """Legt ein Mini-venv-Layout an, gerade genug für find_venv_for."""
    venv = parent / name
    bin_dir = venv / _BIN
    bin_dir.mkdir(parents=True)
    (bin_dir / _PY).write_bytes(b"#!/fake-python\n")
    return venv


def test_find_venv_in_cwd(tmp_path: Path) -> None:
    venv = _make_fake_venv(tmp_path)
    assert find_venv_for(tmp_path) == venv


def test_find_venv_walks_upward(tmp_path: Path) -> None:
    """Worktree unter <repo>/.forge/worktrees/<id>/ findet venv im Repo-Root."""
    venv = _make_fake_venv(tmp_path)
    worktree = tmp_path / ".forge" / "worktrees" / "01ABC"
    worktree.mkdir(parents=True)
    assert find_venv_for(worktree) == venv


def test_find_venv_prefers_dot_venv(tmp_path: Path) -> None:
    """Wenn beide existieren, gewinnt .venv (suchreihenfolge)."""
    dot = _make_fake_venv(tmp_path, ".venv")
    _make_fake_venv(tmp_path, "venv")
    assert find_venv_for(tmp_path) == dot


def test_find_venv_returns_none_without_match(tmp_path: Path) -> None:
    assert find_venv_for(tmp_path) is None


def test_find_venv_ignores_dir_without_python_binary(tmp_path: Path) -> None:
    """Ein leeres .venv/-Verzeichnis ist KEINE valide venv."""
    (tmp_path / ".venv" / _BIN).mkdir(parents=True)
    assert find_venv_for(tmp_path) is None


def test_venv_aware_env_prepends_path(tmp_path: Path) -> None:
    venv = _make_fake_venv(tmp_path)
    base = {"PATH": "/usr/bin:/bin", "HOME": "/home/x"}
    env = venv_aware_env(tmp_path, base)
    bin_str = str(venv / _BIN)
    assert env["PATH"].startswith(bin_str + os.pathsep)
    assert env["VIRTUAL_ENV"] == str(venv)
    assert env["HOME"] == "/home/x"  # andere keys bleiben


def test_venv_aware_env_no_venv_returns_unchanged(tmp_path: Path) -> None:
    base = {"PATH": "/usr/bin", "FOO": "bar"}
    env = venv_aware_env(tmp_path, base)
    assert env["PATH"] == "/usr/bin"
    assert env["FOO"] == "bar"
    assert "VIRTUAL_ENV" not in env


def test_venv_aware_env_idempotent(tmp_path: Path) -> None:
    _make_fake_venv(tmp_path)
    base = {"PATH": "/usr/bin"}
    once = venv_aware_env(tmp_path, base)
    twice = venv_aware_env(tmp_path, once)
    assert once["PATH"] == twice["PATH"]
