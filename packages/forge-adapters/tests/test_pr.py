"""Tests für forge_adapters.github.pr."""

from __future__ import annotations

from decimal import Decimal

import pytest
from forge_adapters.github.pr import (
    GitHubError,
    _extract_pr_number,
    render_pr_body,
)


def test_render_pr_body_minimal() -> None:
    body = render_pr_body(
        run_id="01HZX001",
        focus="legacy_test_revival",
        decision="pr_created",
        final_score=0.81,
        score_delta=0.04,
        total_cost_usd=Decimal("0.94"),
        files_changed=["src/calc.py"],
        generations_count=3,
        factory_version="git:abc123",
    )
    assert "## forge auto-PR" in body
    assert "01HZX001" in body
    assert "legacy_test_revival" in body
    assert "0.8100" in body or "0.8100" in body or "0.81" in body
    assert "+0.0400" in body or "0.04" in body
    assert "src/calc.py" in body
    assert "git:abc123" in body
    assert "Auto-merge is categorically disabled" in body


def test_render_pr_body_no_focus_no_delta() -> None:
    body = render_pr_body(
        run_id="r1",
        focus=None,
        decision="pr_created",
        final_score=None,
        score_delta=None,
        total_cost_usd=Decimal("0"),
        files_changed=[],
        generations_count=1,
        factory_version="pkg:0.1.0",
    )
    assert "manual" in body
    assert "—" in body  # placeholder for missing values


def test_render_pr_body_truncates_files_changed() -> None:
    files = [f"src/file_{i}.py" for i in range(40)]
    body = render_pr_body(
        run_id="r1",
        focus="x",
        decision="pr_created",
        final_score=0.5,
        score_delta=0.01,
        total_cost_usd=Decimal("0"),
        files_changed=files,
        generations_count=1,
        factory_version="git:abc",
    )
    assert "src/file_29.py" in body
    assert "and 10 more" in body


def test_render_pr_body_includes_diff_excerpt() -> None:
    body = render_pr_body(
        run_id="r1",
        focus="x",
        decision="pr_created",
        final_score=0.5,
        score_delta=0.01,
        total_cost_usd=Decimal("0"),
        files_changed=["a.py"],
        generations_count=1,
        factory_version="git:abc",
        diff_excerpt="--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    assert "```diff" in body
    assert "+++ b/a.py" in body


def test_extract_pr_number_from_url() -> None:
    assert _extract_pr_number("https://github.com/owner/repo/pull/42") == 42
    assert _extract_pr_number(
        "Creating pull request...\nhttps://github.com/owner/repo/pull/123\n"
    ) == 123


def test_extract_pr_number_invalid() -> None:
    with pytest.raises(GitHubError):
        _extract_pr_number("no number here")
