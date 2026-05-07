"""Tests für _run_context."""

from __future__ import annotations

from forge_execute._run_context import (
    NO_MORE_TO_DO_SIGNAL,
    RunContext,
    proposal_signals_done,
)


def test_empty_context_renders_nothing() -> None:
    ctx = RunContext()
    assert ctx.render_prompt_addendum() == ""
    assert not ctx.has_history()


def test_record_kept_diff() -> None:
    ctx = RunContext()
    ctx.record_keep(generation_idx=0, diff="diff content")
    assert ctx.has_history()
    rendered = ctx.render_prompt_addendum()
    assert "Generation 0 (KEPT)" in rendered
    assert "diff content" in rendered


def test_empty_diff_not_recorded() -> None:
    ctx = RunContext()
    ctx.record_keep(generation_idx=0, diff="")
    ctx.record_keep(generation_idx=1, diff="   ")
    assert not ctx.has_history()


def test_multiple_diffs_in_order() -> None:
    ctx = RunContext()
    ctx.record_keep(generation_idx=0, diff="first")
    ctx.record_keep(generation_idx=2, diff="third")
    rendered = ctx.render_prompt_addendum()
    pos_first = rendered.find("Generation 0")
    pos_third = rendered.find("Generation 2")
    assert pos_first < pos_third
    assert "first" in rendered
    assert "third" in rendered


def test_long_diff_truncated() -> None:
    ctx = RunContext()
    big_diff = "x" * 10_000
    ctx.record_keep(generation_idx=0, diff=big_diff)
    rendered = ctx.render_prompt_addendum()
    assert "truncated" in rendered
    assert len(rendered) < 6_000  # ungefähr — addendum-Header + truncated diff


def test_addendum_contains_done_signal_instruction() -> None:
    ctx = RunContext()
    ctx.record_keep(generation_idx=0, diff="anything")
    rendered = ctx.render_prompt_addendum()
    assert NO_MORE_TO_DO_SIGNAL in rendered


def test_done_signal_detection_plain() -> None:
    assert proposal_signals_done("forge: nothing more to do") is True
    assert proposal_signals_done("FORGE: NOTHING MORE TO DO") is True


def test_done_signal_detection_with_markdown() -> None:
    assert proposal_signals_done("**forge: nothing more to do**") is True
    assert proposal_signals_done("`forge: nothing more to do`") is True
    assert proposal_signals_done("Result: _forge: nothing more to do_") is True


def test_done_signal_negative() -> None:
    assert proposal_signals_done("") is False
    assert proposal_signals_done("All tests pass.") is False
    assert proposal_signals_done("no work for me") is False
