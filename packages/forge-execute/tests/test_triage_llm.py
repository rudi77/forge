"""Tests für die Parser-Helpers von LLMTriager.

Der tatsächliche claude-Subprozess-Aufruf wird hier nicht abgedeckt —
das ist Integration-Surface, die im board-loop-Test mit einem
FakeTriager isoliert wird.
"""

from __future__ import annotations

import pytest
from forge_execute.triage.base import TriageError
from forge_execute.triage.llm import (
    _decimal_from_any,
    _extract_decision_json,
    _maybe_int,
    _maybe_str,
    _parse_claude_json,
)


def test_parse_claude_json_happy_path() -> None:
    raw = _parse_claude_json('{"result": "ok", "num_turns": 3}')
    assert raw["result"] == "ok"
    assert raw["num_turns"] == 3


def test_parse_claude_json_rejects_empty() -> None:
    with pytest.raises(TriageError, match="no stdout"):
        _parse_claude_json("   \n  ")


def test_parse_claude_json_rejects_garbage() -> None:
    with pytest.raises(TriageError, match="not valid JSON"):
        _parse_claude_json("not-json")


def test_extract_decision_json_picks_last_block() -> None:
    text = (
        "Some intro text.\n"
        "```json\n{\"decision\": \"stale\", \"reason\": \"first\"}\n```\n"
        "Then more reasoning…\n"
        "```json\n{\"decision\": \"relevant\", \"reason\": \"final\"}\n```\n"
    )
    result = _extract_decision_json(text)
    assert result == {"decision": "relevant", "reason": "final"}


def test_extract_decision_json_returns_none_when_no_block() -> None:
    assert _extract_decision_json("plain text, no fences") is None


def test_extract_decision_json_returns_none_on_invalid_json_inside_fence() -> None:
    text = "```json\nnot-valid\n```"
    assert _extract_decision_json(text) is None


def test_extract_decision_json_rejects_non_object() -> None:
    text = '```json\n["array", "not", "object"]\n```'
    assert _extract_decision_json(text) is None


def test_maybe_int_coerces_strings_and_numbers() -> None:
    assert _maybe_int(42) == 42
    assert _maybe_int("17") == 17
    assert _maybe_int(None) is None
    assert _maybe_int("not-a-number") is None
    assert _maybe_int([]) is None


def test_maybe_str_trims_and_handles_none() -> None:
    assert _maybe_str("  abc123  ") == "abc123"
    assert _maybe_str("") is None
    assert _maybe_str("   ") is None
    assert _maybe_str(None) is None
    assert _maybe_str(42) == "42"


def test_decimal_from_any_handles_garbage_gracefully() -> None:
    assert _decimal_from_any("0.42") == _decimal_from_any("0.42")
    assert str(_decimal_from_any(None)) == "0"
    assert str(_decimal_from_any("not-a-decimal")) == "0"
