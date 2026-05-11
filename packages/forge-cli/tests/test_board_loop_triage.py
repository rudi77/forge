"""Tests für die Triage-Choreografie im board-loop.

Wir testen ``_run_triage`` direkt mit einem ``FakeTriager``, der vorab
festgelegte Ergebnisse liefert — der echte ``LLMTriager`` ist Subprozess-
schwer und durch eigene Parser-Tests in ``test_triage_llm.py`` abgedeckt.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from forge_adapters.github import ReadyIssue
from forge_cli import board_loop as bl
from forge_cli.runtime import ForgeContext
from forge_core.events import EventKind, TriageDecision
from forge_core.spec import (
    BoardConfig,
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    TriageConfig,
)
from forge_core.store import EventStore
from forge_execute.capabilities import Capabilities
from forge_execute.triage import TriageResult
from forge_execute.triage.base import TriageError


@dataclass
class _FakeTriager:
    """Triager, der nichts aufruft. Returns the canned result, or raises."""

    canned: TriageResult | None = None
    raises: Exception | None = None
    called: list[ReadyIssue] | None = None

    def triage(self, *, issue: ReadyIssue, repo_root: Path) -> TriageResult:
        if self.called is None:
            self.called = []
        self.called.append(issue)
        if self.raises is not None:
            raise self.raises
        assert self.canned is not None
        return self.canned


def _make_ctx(tmp_path: Path) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0",
        name="testproj",
        cost_caps=CostCapsConfig(
            per_generation_usd=Decimal("0.5"),
            per_run_usd=Decimal("2.0"),
            per_project_per_day_usd=Decimal("10.0"),
            per_project_per_month_usd=Decimal("50.0"),
        ),
        capabilities=CapabilitiesConfig(),
        board=BoardConfig(owner="x", project_number=1),
        triage=TriageConfig(enabled=True, auto_comment=True, auto_close=True),
    )
    return ForgeContext(
        repo_root=tmp_path,
        forge_dir=tmp_path / ".forge",
        spec=spec,
        spec_path=tmp_path / ".forge" / "project.yaml",
        factory_version="git:test",
        project_fingerprint="sha256:test",
        store_path=tmp_path / "events.duckdb",
        blobs_path=tmp_path / "blobs",
    )


def _issue(n: int = 42) -> ReadyIssue:
    return ReadyIssue(
        number=n,
        title=f"issue {n}",
        body="some body",
        labels=["bug"],
        project_status="Ready",
        url=f"https://github.com/x/y/issues/{n}",
    )


def _patch_gh(monkeypatch: Any) -> tuple[MagicMock, MagicMock]:
    """Patcht comment_issue + close_issue zu MagicMocks und liefert sie zurück."""
    fake_comment = MagicMock()
    fake_close = MagicMock()
    monkeypatch.setattr(bl, "comment_issue", fake_comment)
    monkeypatch.setattr(bl, "close_issue", fake_close)
    return fake_comment, fake_close


def test_relevant_outcome_dispatches_and_skips_side_effects(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    triager = _FakeTriager(canned=TriageResult(decision="relevant", reason="fix me"))
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    assert outcome.dispatch is True
    fake_comment.assert_not_called()
    fake_close.assert_not_called()

    # Event wurde geschrieben mit decision=relevant
    store = EventStore(ctx.store_path)
    try:
        events = store.events_by_kind(EventKind.ISSUE_TRIAGED)
    finally:
        store.close()
    assert len(events) == 1
    assert events[0].payload["decision"] == "relevant"
    assert events[0].payload["issue_number"] == 42


@pytest.mark.parametrize(
    "decision,close_reason",
    [
        ("stale", "not planned"),
        ("duplicate", "not planned"),
        ("already_solved", "completed"),
    ],
)
def test_non_relevant_outcome_runs_side_effects_and_skips_dispatch(
    tmp_path: Path, monkeypatch: Any, decision: TriageDecision, close_reason: str
) -> None:
    ctx = _make_ctx(tmp_path)
    triager = _FakeTriager(
        canned=TriageResult(decision=decision, reason="not worth it")
    )
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    assert outcome.dispatch is False
    assert outcome.summary_row.decision == f"triaged_{decision}"

    fake_comment.assert_called_once()
    fake_close.assert_called_once()
    close_kwargs = fake_close.call_args.kwargs
    assert close_kwargs["reason"] == close_reason


def test_triage_error_is_treated_as_relevant_fallback(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    triager = _FakeTriager(raises=TriageError("claude crashed"))
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    # Fallback dispatched normal — der Hauptpfad bleibt intakt.
    assert outcome.dispatch is True
    fake_comment.assert_not_called()
    fake_close.assert_not_called()

    # Event wurde trotzdem geschrieben — als "relevant", reason enthält den Fehler
    store = EventStore(ctx.store_path)
    try:
        events = store.events_by_kind(EventKind.ISSUE_TRIAGED)
    finally:
        store.close()
    assert len(events) == 1
    assert events[0].payload["decision"] == "relevant"
    assert "claude crashed" in events[0].payload["reason"]


def test_auto_close_off_keeps_issue_open(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.spec.triage.auto_close = False
    triager = _FakeTriager(canned=TriageResult(decision="stale", reason="x"))
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    assert outcome.dispatch is False
    fake_comment.assert_called_once()  # comment still posted
    fake_close.assert_not_called()


def test_auto_comment_off_skips_comment(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.spec.triage.auto_comment = False
    triager = _FakeTriager(canned=TriageResult(decision="duplicate", reason="x"))
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    assert outcome.dispatch is False
    fake_comment.assert_not_called()
    fake_close.assert_called_once()


def test_close_capability_denied_keeps_issue_open(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _make_ctx(tmp_path)
    ctx.spec.capabilities.close_issue = False
    triager = _FakeTriager(canned=TriageResult(decision="stale", reason="x"))
    caps = Capabilities(ctx.spec)
    fake_comment, fake_close = _patch_gh(monkeypatch)

    outcome = bl._run_triage(
        ctx=ctx, issue=_issue(), triager=triager, capabilities=caps
    )
    assert outcome.dispatch is False
    fake_comment.assert_called_once()
    fake_close.assert_not_called()


def test_format_triage_comment_includes_decision_label_and_reason() -> None:
    result = TriageResult(
        decision="duplicate",
        reason="schon im PR #99 erledigt",
        related_pr=99,
    )
    body = bl._format_triage_comment(result)
    assert "Duplikat" in body
    assert "schon im PR #99 erledigt" in body
    assert "#99" in body


def test_emit_triage_event_writes_payload_correctly(tmp_path: Path) -> None:
    ctx = _make_ctx(tmp_path)
    result = TriageResult(
        decision="already_solved",
        reason="fixed in main",
        related_commit="abc1234",
        turns_used=3,
        cost_usd=Decimal("0.04"),
        model="claude-haiku-4-5",
    )
    bl._emit_triage_event(ctx=ctx, issue=_issue(7), result=result, run_id="r-test")
    store = EventStore(ctx.store_path)
    try:
        events = store.events_by_kind(EventKind.ISSUE_TRIAGED)
    finally:
        store.close()
    assert len(events) == 1
    evt = events[0]
    assert evt.run_id == "r-test"
    assert evt.payload["decision"] == "already_solved"
    assert evt.payload["related_commit"] == "abc1234"
    assert evt.payload["turns_used"] == 3
    assert evt.cost_usd == Decimal("0.04")
    assert evt.model == "claude-haiku-4-5"
