"""Tests für die backslash-newline-Normalisierung im CommandEvaluator."""

from __future__ import annotations

from forge_execute.evaluators.command import _normalize_line_continuations


def test_single_continuation_becomes_space() -> None:
    cmd = "pytest \\\ntests/test_x.py"
    assert _normalize_line_continuations(cmd) == "pytest tests/test_x.py"


def test_multiple_continuations() -> None:
    cmd = "pytest \\\n  tests/a.py \\\n  tests/b.py"
    assert _normalize_line_continuations(cmd) == "pytest tests/a.py tests/b.py"


def test_crlf_continuations() -> None:
    cmd = "pytest \\\r\n  tests/a.py"
    assert _normalize_line_continuations(cmd) == "pytest tests/a.py"


def test_no_continuation_unchanged() -> None:
    cmd = "pytest tests/a.py tests/b.py"
    assert _normalize_line_continuations(cmd) == "pytest tests/a.py tests/b.py"


def test_strips_outer_whitespace() -> None:
    cmd = "  pytest x.py  \n"
    assert _normalize_line_continuations(cmd) == "pytest x.py"


def test_preserves_pipes_and_redirects() -> None:
    """Pipes und Redirects bleiben unangetastet — die sind keine Continuations."""
    cmd = "pytest | grep PASSED > out.txt"
    assert _normalize_line_continuations(cmd) == "pytest | grep PASSED > out.txt"
