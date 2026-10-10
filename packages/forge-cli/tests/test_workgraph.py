"""Roadmap G: Arbeitsgraph über Worktrees — Touches, Konfliktkanten, Kapazität,
Geschwister nachziehen, Integrations-Branch."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli import workgraph
from forge_cli.conductor import (
    DispatchOrder,
    WorkItem,
    derive_signals,
    effective_capacity,
    plan_tick,
)
from forge_cli.dependencies import (
    parse_integration_branch,
    parse_integration_mode,
    parse_touches,
    touches_overlap,
)
from forge_cli.runtime import ForgeContext
from forge_cli.stages import Stage, StageSignals, advance
from forge_core.events import EventKind as EK
from forge_core.spec import BoardConfig, CostCapsConfig, ProjectSpec, TriageConfig
from forge_core.tracking import ReadyIssue

# --- Parser + Überlappung ---------------------------------------------------------


def test_parse_touches_and_integration_lines() -> None:
    body = "x\nTouches: `src/auth/**`, tests/auth/**\nIntegration: branch\n"
    assert parse_touches(body) == ["src/auth/**", "tests/auth/**"]
    assert parse_touches("nothing") == []
    assert parse_integration_mode(body) == "branch"
    assert parse_integration_mode("") == "direct"
    assert parse_integration_branch("Integration-Branch: forge/epic-3") == "forge/epic-3"


@pytest.mark.parametrize(
    "a,b,overlap",
    [
        (["src/auth/**"], ["src/auth/login.py"], True),
        (["src/auth/**"], ["src/billing/**"], False),
        (["**/*.py"], ["docs/x.md"], True),
        (["web/a.js"], ["web/a.js"], True),
        (["web/a.js"], ["web/b.js"], False),
    ],
)
def test_touches_overlap(a, b, overlap) -> None:
    assert touches_overlap(a, b) is overlap


# --- plan_tick: Konfliktkanten ------------------------------------------------------


def _ready(n: int, *touches: str) -> WorkItem:
    return WorkItem(n, Stage.READY, touches=tuple(touches))


def test_disjoint_items_run_in_parallel() -> None:
    plan = plan_tick([_ready(1, "src/a/**"), _ready(2, "src/b/**")], capacity=3)
    assert [o.number for o in plan.dispatch] == [1, 2]


def test_overlapping_items_are_serialised_by_number() -> None:
    plan = plan_tick([_ready(1, "src/a/**"), _ready(2, "src/a/x.py")], capacity=3)
    assert [o.number for o in plan.dispatch] == [1]
    (b,) = plan.blocked
    assert (b.number, b.kind, b.blocked_by) == (2, "file_conflict", (1,))


def test_item_in_flight_blocks_overlapping_ready_item() -> None:
    in_flight = WorkItem(1, Stage.QA, touches=("src/a/**",),
                         signals=StageSignals(has_open_pr=True, review_done=True))
    plan = plan_tick([in_flight, _ready(2, "src/a/y.py")], capacity=3)
    assert plan.dispatch == []
    assert plan.blocked[0].kind == "file_conflict"


def test_item_without_touches_runs_alone() -> None:
    plan = plan_tick([_ready(1), _ready(2, "src/b/**")], capacity=3)
    assert [o.number for o in plan.dispatch] == [1]
    plan = plan_tick([_ready(1, "src/a/**"), _ready(2)], capacity=3)
    assert [o.number for o in plan.dispatch] == [1]


def test_non_code_runs_do_not_compete_for_files() -> None:
    plan = plan_tick([WorkItem(1, Stage.DESIGN), _ready(2), _ready(3, "x/**")], capacity=5)
    assert [(o.number, o.stage) for o in plan.dispatch] == [
        (1, Stage.DESIGN), (2, Stage.IN_DEV),
    ]


def test_capacity_still_limits() -> None:
    plan = plan_tick([_ready(1, "a/**"), _ready(2, "b/**"), _ready(3, "c/**")], capacity=2)
    assert len(plan.dispatch) == 2


# --- Kapazität ------------------------------------------------------------------------


def test_effective_capacity() -> None:
    assert effective_capacity(4, daily_cap_usd=10, spent_today_usd=0, avg_run_cost_usd=None) == 4
    assert effective_capacity(4, daily_cap_usd=10, spent_today_usd=7, avg_run_cost_usd=1.5) == 2
    assert effective_capacity(4, daily_cap_usd=10, spent_today_usd=9.5, avg_run_cost_usd=2) == 1
    assert effective_capacity(4, daily_cap_usd=10, spent_today_usd=10, avg_run_cost_usd=1) == 0
    assert effective_capacity(4, daily_cap_usd=10, spent_today_usd=0, avg_run_cost_usd=None,
                              disk_free_bytes=10) == 1


# --- Merge-Konflikte ------------------------------------------------------------------


def test_conflicting_pr_goes_back_to_in_dev_and_syncs() -> None:
    sig = StageSignals(has_open_pr=True, conflicting=True)
    assert advance(Stage.QA, sig) == (Stage.IN_DEV, "merge_conflict")
    assert advance(Stage.IN_DEV, sig) == (Stage.IN_DEV, "")
    item = WorkItem(5, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, conflicting=True, changes_requested=True))
    assert plan_tick([item], capacity=1).dispatch == [DispatchOrder(5, Stage.IN_DEV, "sync")]
    failed = WorkItem(5, Stage.IN_DEV, signals=StageSignals(
        has_open_pr=True, conflicting=True, conflict_fix_started=True, conflict_fix_failed=True))
    plan = plan_tick([failed], capacity=1)
    assert [b.kind for b in plan.blocked] == ["merge_conflict"]


class _Evt:
    def __init__(self, kind, run_id, payload, ts):
        self.kind, self.run_id, self.payload, self.ts = kind, run_id, payload, ts


def test_conflict_runs_are_tracked_separately_from_rework() -> None:
    t = lambda h: datetime(2026, 6, 1, h, tzinfo=UTC)  # noqa: E731
    events = [
        _Evt(EK.RUN_STARTED, "r1", {"issue_number": 9}, t(1)),
        _Evt(EK.PR_CREATED, "r1", {"pr_number": 50}, t(2)),
        _Evt(EK.PR_REVIEWED, "rv", {"pr_number": 50, "verdict": "request_changes"}, t(3)),
        _Evt(EK.RUN_STARTED, "c1", {"issue_number": 9, "trigger": "rework",
                                    "focus": "conflict:#9"}, t(5)),
    ]
    sig = derive_signals(events, 9, head_committed_at=t(4), mergeable="CONFLICTING")
    assert sig.conflicting and sig.conflict_fix_started
    assert sig.rework_started is False  # Konflikt-Run ist keine Nacharbeit


# --- sync_branch gegen echtes git ------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def remote_setup(tmp_path: Path):
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "a.txt").write_text("one\ntwo\nthree\n")
    (seed / "b.txt").write_text("b\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    _git(seed, "checkout", "-q", "-b", "forge/pr")
    (seed / "a.txt").write_text("one\nTWO-pr\nthree\n")
    _git(seed, "commit", "-q", "-am", "pr change")
    _git(seed, "push", "-q", "origin", "forge/pr")
    _git(seed, "checkout", "-q", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    return seed, clone


def _ctx_for(repo: Path, code_host) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0", name="p",
        cost_caps=CostCapsConfig(per_generation_usd=Decimal("1"), per_run_usd=Decimal("1"),
                                 per_project_per_day_usd=Decimal("10"),
                                 per_project_per_month_usd=Decimal("10")),
        board=BoardConfig(owner="x", project_number=1), triage=TriageConfig(enabled=False),
    )
    return ForgeContext(
        repo_root=repo, forge_dir=repo / ".forge", spec=spec,
        spec_path=repo / ".forge" / "p.yaml", factory_version="git:t",
        project_fingerprint="sha256:t", store_path=repo / ".forge" / "e.duckdb",
        blobs_path=repo / ".forge" / "blobs", tracker=InMemoryTracker(), code_host=code_host,
    )


def _host_with_pr(clone: Path) -> Any:
    from forge_adapters.github import GitHubCodeHost

    host = GitHubCodeHost(repo_root=clone)
    meta = type("M", (), {"head_branch": "forge/pr", "base_branch": "main"})()
    host.fetch_metadata = lambda n: meta  # type: ignore[method-assign]
    return host


def test_sync_branch_clean_merge_pushes_fast_forward(remote_setup) -> None:
    seed, clone = remote_setup
    (seed / "b.txt").write_text("b sibling\n")
    _git(seed, "commit", "-q", "-am", "sibling merged")
    _git(seed, "push", "-q", "origin", "main")
    ctx = _ctx_for(clone, _host_with_pr(clone))
    res = workgraph.sync_branch(ctx, 7)
    assert res.status == "synced"
    _git(seed, "fetch", "-q", "origin")
    content = _git(seed, "show", "origin/forge/pr:b.txt")
    assert content == "b sibling"
    assert _git(seed, "show", "origin/forge/pr:a.txt").splitlines()[1] == "TWO-pr"


def test_sync_branch_conflict_leaves_local_conflict_commit(remote_setup) -> None:
    seed, clone = remote_setup
    (seed / "a.txt").write_text("one\nTWO-main\nthree\n")
    _git(seed, "commit", "-q", "-am", "sibling conflicts")
    _git(seed, "push", "-q", "origin", "main")
    ctx = _ctx_for(clone, _host_with_pr(clone))
    res = workgraph.sync_branch(ctx, 7)
    assert res.status == "conflict"
    assert res.files == ("a.txt",)
    assert "<<<<<<<" in _git(clone, "show", f"{res.conflict_ref}:a.txt")
    # Nichts wurde auf den PR-Branch gepusht.
    _git(seed, "fetch", "-q", "origin")
    assert "<<<<<<<" not in _git(seed, "show", "origin/forge/pr:a.txt")


def test_sync_branch_skips_human_branches(tmp_path: Path) -> None:
    host = InMemoryCodeHost()
    pr = host.open_change(branch="feature/x", title="t", body="b", push=False)
    ctx = _ctx_for(tmp_path, host)
    assert workgraph.sync_branch(ctx, pr.pr_number).status == "skipped"


def test_dispatch_sync_runs_conflict_agent_from_conflict_commit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    ctx = _ctx_for(tmp_path, InMemoryCodeHost())
    monkeypatch.setattr(bl, "sync_branch", lambda ctx, n: workgraph.SyncResult(
        "conflict", conflict_ref="forge/sync-7", files=("a.txt",), head_branch="forge/pr"))
    calls: list[dict] = []
    monkeypatch.setattr(bl, "_dispatch_branch_run", lambda **kw: calls.append(kw) or
                        bl._PassResult([], False, 1, 0))
    issue = ReadyIssue(3, "t", "b", ["forge:in-dev"], "", "")
    bl._dispatch_sync(ctx=ctx, issue=issue, pr_number=7, params=_params())
    assert calls[0]["kind"] == "conflict"
    assert calls[0]["base_ref_override"] == "forge/sync-7"
    assert "a.txt" in calls[0]["context"]


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )


# --- Integrations-Branch ---------------------------------------------------------------


def test_integration_child_is_released_into_epic_branch(tmp_path: Path) -> None:
    ctx = _ctx_for(tmp_path, InMemoryCodeHost())
    issue = ReadyIssue(4, "child", "AC\n\nIntegration-Branch: forge/epic-2", ["forge:release"],
                       "", "")
    store = ctx.open_store()
    res = bl._dispatch_release_run(ctx=ctx, issue=issue, store=store, session_id="s")
    (evt,) = store.events_by_kind(EK.RELEASE_TAGGED)
    store.close()
    assert res.summaries[0].decision == "integrated"
    assert evt.payload["integrated_into"] == "forge/epic-2"
    started = _Evt(EK.RUN_STARTED, "dev4", {"issue_number": 4}, evt.ts)
    assert derive_signals([started, evt], 4).release_done is True
    assert ctx.code_host.releases == {}  # kein Tag/Release für ein Kind


def test_integration_epic_opens_pr_when_children_done_then_goes_to_qa(
    tmp_path: Path, monkeypatch: Any
) -> None:
    from forge_core.events import RunStartedPayload, WorkItemCreatedPayload, build_event

    ctx = _ctx_for(tmp_path, InMemoryCodeHost())
    ctx.tracker.add(ReadyIssue(1, "Epic", "Integration: branch", ["forge:tracking"], "", ""))
    ctx.tracker.add(ReadyIssue(2, "c1", "", ["forge:done"], "", ""))
    ctx.tracker.add(ReadyIssue(3, "c2", "", ["forge:done"], "", ""))
    common = dict(project="p", project_fingerprint="sha256:t", factory_version="git:t",
                  spec_version="1.0")
    store = ctx.open_store()
    store.append(build_event(kind=EK.RUN_STARTED, run_id="epic1", payload=RunStartedPayload(
        trigger="issue_label", strategy="sequential", config_hash="c", issue_number=1,
        focus="epic:#1"), **common))
    for n in (2, 3):
        store.append(build_event(kind=EK.WORK_ITEM_CREATED, run_id="epic1",
                                 payload=WorkItemCreatedPayload(
                                     number=n, kind="story", title=f"c{n}",
                                     source="epic_decomposition", stage="forge:requirements",
                                     parent=1, fingerprint="sha256:" + "0" * 64),
                                 **common))
    store.close()

    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1)
    (pr,) = ctx.code_host.prs.values()
    assert pr.branch == "forge/epic-1" and pr.base == "main"
    assert "forge:qa" in ctx.tracker.labels_of(1)  # tracking → qa (integration_pr)


def test_tick_records_capacity_and_parallelism(tmp_path: Path, monkeypatch: Any) -> None:
    ctx = _ctx_for(tmp_path, InMemoryCodeHost())
    ctx.tracker.add(ReadyIssue(1, "a", "Touches: a/**", ["forge:ready"], "", ""))
    ctx.tracker.add(ReadyIssue(2, "b", "Touches: b/**", ["forge:ready"], "", ""))
    monkeypatch.setattr(bl, "_dispatch_issues", lambda **kw: bl._PassResult([], False, 1, 0))
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1, max_parallel=2)
    store = ctx.open_store()
    (tick,) = store.events_by_kind(EK.CONDUCTOR_TICK_COMPLETED)
    rows = store.query("SELECT max_parallel FROM factory_parallelism")
    store.close()
    assert tick.payload["parallel_running"] == 2
    assert tick.payload["capacity"] == 2
    assert rows[0]["max_parallel"] == 2
