"""Roadmap A: Arbeit erzeugen — intake (rein), Stages/Signale, workgen, Conductor."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli import workgen
from forge_cli.conductor import (
    DispatchOrder,
    WorkItem,
    derive_signals,
    epic_children,
    plan_tick,
    pr_number_for_issue,
    spec_pr_for_issue,
)
from forge_cli.intake import (
    ProposedItem,
    create_items,
    creation_order,
    fingerprint,
    parse_workitems,
    start_stage,
)
from forge_cli.runtime import ForgeContext
from forge_cli.stages import Stage, StageSignals, advance, stage_of
from forge_core.events import EventKind as EK
from forge_core.spec import (
    BoardConfig,
    CostCapsConfig,
    IntakeConfig,
    ProjectSpec,
    ScheduleTriggerConfig,
    TriageConfig,
)
from forge_core.tracking import ReadyIssue

BLOCK = """
- id: api
  kind: story
  title: Add login endpoint
  touches: [src/auth/**]
  body: |
    POST /login returns a token.
- id: ui
  kind: story
  title: Login form
  depends_on: [api]
  touches: [web/login/**]
"""


# --- intake (rein) ----------------------------------------------------------------


def test_parse_workitems_reads_yaml_and_fences() -> None:
    items = parse_workitems("```yaml\n" + BLOCK + "\n```")
    assert [i.local_id for i in items] == ["api", "ui"]
    assert items[1].depends_on == ("api",)
    assert items[0].touches == ("src/auth/**",)
    assert "token" in items[0].body


def test_parse_workitems_is_fail_open() -> None:
    assert parse_workitems(None) == []
    assert parse_workitems("::: not yaml [") == []
    assert parse_workitems("- kind: bug") == []  # ohne Titel unbrauchbar
    weird = parse_workitems("- title: x\n  kind: rocket\n  depends_on: a, b")
    assert weird[0].kind == "task" and weird[0].depends_on == ("a", "b")


def test_fingerprint_is_stable_and_normalised() -> None:
    assert fingerprint("run_finding", "Null  pointer!") == fingerprint("run_finding", "null pointer")
    assert fingerprint("run_finding", "x") != fingerprint("schedule", "x")
    assert fingerprint("s", "x", "2026-01-01") != fingerprint("s", "x", "2026-01-02")


def test_creation_order_puts_dependencies_first_and_breaks_cycles() -> None:
    a = ProposedItem("a", "task", "A", depends_on=("b",))
    b = ProposedItem("b", "task", "B")
    assert [i.local_id for i in creation_order([a, b])] == ["b", "a"]
    x = ProposedItem("x", "task", "X", depends_on=("y",))
    y = ProposedItem("y", "task", "Y", depends_on=("x",))
    out = creation_order([x, y])
    assert {i.local_id for i in out} == {"x", "y"}
    assert all(i.depends_on == () for i in out)


def test_start_stage_gates_unapproved_items() -> None:
    cfg = IntakeConfig()
    assert start_stage("bug", source="run_finding", cfg=cfg) == "forge:proposed"
    assert start_stage("bug", source="run_finding", cfg=IntakeConfig(auto_accept=["bug"])) == (
        "forge:requirements"
    )
    assert start_stage("story", source="epic_decomposition", cfg=cfg, parent_approved=True) == (
        "forge:requirements"
    )
    assert start_stage("epic", source="schedule", cfg=IntakeConfig(auto_accept=["epic"])) == (
        "forge:epic"
    )


def _create(tracker, items, **kw):
    emitted: list[dict] = []
    defaults = dict(tracker=tracker, emit=emitted.append, cfg=IntakeConfig(), allowed=True,
                    source="epic_decomposition", created_today=0)
    defaults.update(kw)
    return create_items(items, **defaults), emitted


def test_create_items_resolves_dependencies_to_real_numbers() -> None:
    tracker = InMemoryTracker([ReadyIssue(1, "epic", "", ["forge:tracking"], "", "")])
    res, emitted = _create(tracker, parse_workitems(BLOCK), parent=1, parent_approved=True)
    api, ui = res.created
    body = tracker.items[ui].issue.body
    assert f"Depends-On: #{api}" in body
    assert "Touches: web/login/**" in body
    assert "forge-fingerprint: sha256:" in body
    assert tracker.labels_of(api)[:2] == ["forge:requirements", "forge:generated"]
    assert emitted[1]["depends_on"] == [api] and emitted[1]["parent"] == 1


def test_create_items_respects_capability_limits_and_dedupe() -> None:
    tracker = InMemoryTracker()
    items = parse_workitems(BLOCK)
    res, _ = _create(tracker, items, allowed=False)
    assert res.created == [] and len(res.skipped) == 2

    res, _ = _create(tracker, items, cfg=IntakeConfig(max_items_per_day=5), created_today=4)
    assert len(res.created) == 1 and "limit" in res.skipped[0]

    again, emitted = _create(tracker, items)
    assert again.deduplicated == res.created
    assert len(again.created) == 1  # nur das vorher abgeschnittene Item kommt neu
    assert len(emitted) == 1


# --- Stages / Signale ------------------------------------------------------------


def test_stage_of_recognises_factory_stages() -> None:
    assert stage_of(["forge:proposed"]) == Stage.PROPOSED
    assert stage_of(["forge:epic"]) == Stage.EPIC
    # Freigabe durch einen Menschen: Pipeline-Label daneben gewinnt.
    assert stage_of(["forge:proposed", "forge:requirements"]) == Stage.REQUIREMENTS


def test_advance_epic_and_tracking_and_spec_gate() -> None:
    assert advance(Stage.EPIC, StageSignals(has_decomposition=True)) == (
        Stage.TRACKING, "epic_decomposed")
    assert advance(Stage.TRACKING, StageSignals(children_done=True)) == (
        Stage.DONE, "children_done")
    assert advance(Stage.REQUIREMENTS, StageSignals(has_refined_spec=True, spec_pending=True)) == (
        Stage.REQUIREMENTS, "")


def test_plan_tick_never_touches_proposed_and_dispatches_epic_once() -> None:
    plan = plan_tick([WorkItem(1, Stage.PROPOSED)], capacity=3)
    assert plan.dispatch == [] and plan.transitions == []
    plan = plan_tick([WorkItem(2, Stage.EPIC)], capacity=3)
    assert plan.dispatch == [DispatchOrder(2, Stage.EPIC)]
    plan = plan_tick([WorkItem(3, Stage.REQUIREMENTS, signals=StageSignals(
        has_refined_spec=True, spec_pending=True))], capacity=3)
    assert plan.dispatch == []  # wartet auf den Spec-PR-Merge, kein neuer Run
    plan = plan_tick([WorkItem(4, Stage.EPIC, signals=StageSignals(epic_failed_runs=2))],
                     capacity=3)
    assert plan.blocked and plan.transitions[0].to_stage == Stage.BLOCKED


class _Evt:
    def __init__(self, kind, run_id, payload, ts=None):
        self.kind, self.run_id, self.payload = kind, run_id, payload
        self.ts = ts or datetime(2026, 6, 1, tzinfo=UTC)


def test_spec_prs_never_count_as_code_prs() -> None:
    events = [
        _Evt(EK.RUN_STARTED, "req", {"issue_number": 5, "focus": "requirements:#5"}),
        _Evt(EK.REQUIREMENTS_REFINED, "req", {"issue_number": 5}),
        _Evt(EK.PR_CREATED, "req", {"pr_number": 9, "labels": ["forge:spec", "forge:auto"]}),
    ]
    sig = derive_signals(events, 5)
    assert sig.spec_pending is True and sig.has_open_pr is False
    assert pr_number_for_issue(events, 5) is None
    assert spec_pr_for_issue(events, 5) == 9
    events.append(_Evt(EK.PR_MERGED, "s", {"pr_number": 9}))
    sig = derive_signals(events, 5)
    assert sig.spec_pending is False and sig.has_merged_pr is False


def test_epic_signals_from_work_item_created() -> None:
    events = [
        _Evt(EK.RUN_STARTED, "e1", {"issue_number": 1, "focus": "epic:#1"}),
        _Evt(EK.RUN_FINISHED, "e1", {"decision": "no_improvement"}),
    ]
    assert derive_signals(events, 1).epic_failed_runs == 1
    events.append(_Evt(EK.WORK_ITEM_CREATED, "e1", {"number": 7, "parent": 1,
                                                     "source": "epic_decomposition"}))
    sig = derive_signals(events, 1)
    assert sig.has_decomposition and sig.epic_failed_runs == 0
    assert epic_children(events, 1) == [7]


# --- workgen (Effekte) ------------------------------------------------------------


def _ctx(tmp_path: Path, **spec_kw) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0", name="p",
        cost_caps=CostCapsConfig(
            per_generation_usd=Decimal("0.5"), per_run_usd=Decimal("2"),
            per_project_per_day_usd=Decimal("10"), per_project_per_month_usd=Decimal("50"),
        ),
        board=BoardConfig(owner="x", project_number=1),
        triage=TriageConfig(enabled=False),
        **spec_kw,
    )
    return ForgeContext(
        repo_root=tmp_path, forge_dir=tmp_path / ".forge", spec=spec,
        spec_path=tmp_path / ".forge" / "project.yaml", factory_version="git:test",
        project_fingerprint="sha256:test", store_path=tmp_path / "events.duckdb",
        blobs_path=tmp_path / "blobs", tracker=InMemoryTracker(), code_host=InMemoryCodeHost(),
    )


def _seed_refined_spec(ctx: ForgeContext, issue: int, text: str) -> None:
    from forge_core.events import RequirementsRefinedPayload, RunStartedPayload, build_event

    common = dict(project="p", project_fingerprint="sha256:test",
                  factory_version="git:test", spec_version="1.0")
    blob = ctx.open_blobs().put_text(text)
    store = ctx.open_store()
    store.append(build_event(kind=EK.RUN_STARTED, run_id=f"req{issue}", payload=RunStartedPayload(
        trigger="issue_label", strategy="sequential", config_hash="c", issue_number=issue,
        focus=f"requirements:#{issue}"), **common))
    store.append(build_event(kind=EK.REQUIREMENTS_REFINED, run_id=f"req{issue}",
                             payload=RequirementsRefinedPayload(issue_number=issue),
                             artifacts={"spec": blob}, **common))
    store.close()


def test_publish_spec_as_comment_for_bugs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    issue = ReadyIssue(5, "crash", "b", ["bug", "forge:requirements"], "", "")
    ctx.tracker.add(issue)
    _seed_refined_spec(ctx, 5, "1. no crash on empty input")
    store = ctx.open_store()
    assert workgen.publish_spec(ctx, issue=issue, run_id="req5", store=store) == "comment"
    store.close()
    assert "no crash on empty input" in ctx.tracker.comments_of(5)[0]


def _git_repo(path: Path) -> None:
    def git(*a):
        subprocess.run(["git", *a], cwd=path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    (path / "README.md").write_text("x\n")
    git("add", ".")
    git("commit", "-q", "-m", "init")


def test_publish_spec_as_pr_for_features(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_repo(repo)
    ctx = _ctx(repo)
    issue = ReadyIssue(6, "Login flow", "b", ["forge:requirements"], "", "", kind="feature")
    _seed_refined_spec(ctx, 6, "1. user can log in")
    store = ctx.open_store()
    assert workgen.publish_spec(ctx, issue=issue, run_id="req6", store=store) == "pr"
    events = store.events_by_kind(EK.PR_CREATED)
    store.close()
    (pr,) = ctx.code_host.prs.values()
    assert "forge:spec" in pr.labels and pr.title.startswith("spec: #6")
    assert events[0].run_id == "req6"
    show = subprocess.run(
        ["git", "show", f"{pr.branch}:docs/specs/6-login-flow.md"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout
    assert "user can log in" in show


def test_tick_sources_ci_red_creates_one_bug_per_day(tmp_path: Path) -> None:
    from forge_core.spec import CapabilitiesConfig

    ctx = _ctx(tmp_path, capabilities=CapabilitiesConfig(create_work_items=True),
               intake=IntakeConfig(watch_main_ci="main"))
    ctx.code_host.ref_status["main"] = "fail"
    store = ctx.open_store()
    now = datetime(2026, 6, 1, 12, tzinfo=UTC)
    assert workgen.tick_sources(ctx, store=store, session_id="s", now=now) == 1
    assert workgen.tick_sources(ctx, store=store, session_id="s", now=now) == 0
    store.close()
    (item,) = (f.issue for f in ctx.tracker.items.values())
    assert item.title == "CI is red on main" and "forge:proposed" in item.labels


def test_tick_sources_schedule_creates_epic_when_due(tmp_path: Path) -> None:
    from forge_core.spec import CapabilitiesConfig, TriggersConfig

    ctx = _ctx(
        tmp_path,
        capabilities=CapabilitiesConfig(create_work_items=True),
        intake=IntakeConfig(auto_accept=["epic"]),
        triggers=TriggersConfig(schedule=[
            ScheduleTriggerConfig(cron="0 2 * * *", focus="tech_debt")
        ]),
    )
    store = ctx.open_store()
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    # Ohne Historie zählt das letzte Tagesfenster → ein täglicher Cron feuert
    # beim ersten Tick (Nachholen statt exakter Minuten-Treffer).
    assert workgen.tick_sources(ctx, store=store, session_id="s", now=now) == 1
    # Danach ist der jüngste Lauf der Anker → nicht nochmal im selben Fenster.
    assert workgen.tick_sources(ctx, store=store, session_id="s", now=now) == 0
    store.close()
    (item,) = (f.issue for f in ctx.tracker.items.values())
    assert "forge:epic" in item.labels


def test_tick_sources_noop_without_capability(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, intake=IntakeConfig(watch_main_ci="main"))
    ctx.code_host.ref_status["main"] = "fail"
    store = ctx.open_store()
    assert workgen.tick_sources(ctx, store=store, session_id="s",
                                now=datetime.now(UTC)) == 0
    store.close()


# --- Conductor-Integration: Epic → Kinder → tracking → done ----------------------


def _params() -> bl._DispatchParams:
    return bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )


def test_conductor_decomposes_epic_and_tracks_children(tmp_path: Path, monkeypatch: Any) -> None:
    from forge_core.events import RunStartedPayload, build_event
    from forge_core.spec import CapabilitiesConfig

    ctx = _ctx(tmp_path, capabilities=CapabilitiesConfig(create_work_items=True))
    ctx.tracker.add(ReadyIssue(1, "Login", "users can log in", ["forge:epic"], "", ""))

    def fake_execute_run(**kw):
        store = kw["store"]
        store.append(build_event(
            kind=EK.RUN_STARTED, run_id="epicrun", project="p",
            project_fingerprint="sha256:test", factory_version="git:test", spec_version="1.0",
            payload=RunStartedPayload(trigger="issue_label", strategy="sequential",
                                      config_hash="c", issue_number=1, focus=kw["focus"])))
        result = type("R", (), {"run_id": "epicrun", "decision": "no_improvement",
                                "workitems_blocks": [BLOCK]})()
        return type("O", (), {"result": result})()

    monkeypatch.setattr(bl, "execute_run", fake_execute_run)
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1)
    children = [n for n in ctx.tracker.items if n != 1]
    assert len(children) == 2
    assert all(ctx.tracker.items[c].issue.parent == 1 for c in children)
    assert all("forge:requirements" in ctx.tracker.labels_of(c) for c in children)

    # Tick 2: epic → tracking (Kinder bleiben in requirements, kein Dispatch nötig).
    monkeypatch.setattr(bl, "_dispatch_requirements_run",
                        lambda **kw: bl._PassResult([], False, 0, 0))
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1)
    assert "forge:tracking" in ctx.tracker.labels_of(1)

    # Alle Kinder done → tracking → done.
    for c in children:
        ctx.tracker.set_stage(number=c, add="forge:done", remove="forge:requirements")
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=_params(),
                            triager=None, capabilities=None, max_ticks=1)
    assert "forge:done" in ctx.tracker.labels_of(1)


def test_run_findings_become_proposed_items(tmp_path: Path) -> None:
    from forge_core.spec import CapabilitiesConfig

    ctx = _ctx(tmp_path, capabilities=CapabilitiesConfig(create_work_items=True))
    issue = ReadyIssue(4, "fix parser", "b", ["forge:in-dev"], "", "")
    ctx.tracker.add(issue)
    result = type("R", (), {"run_id": "r4", "workitems_blocks": [
        "- id: x\n  kind: bug\n  title: Race in cache eviction\n"]})()
    bl._intake_run_findings(ctx, None, issue, type("O", (), {"result": result})())
    new = [f.issue for n, f in ctx.tracker.items.items() if n != 4]
    assert len(new) == 1
    assert "forge:proposed" in new[0].labels
    assert "Found while working on #4." in new[0].body
    store = ctx.open_store()
    (evt,) = store.events_by_kind(EK.WORK_ITEM_CREATED)
    store.close()
    assert evt.payload["source"] == "run_finding" and evt.payload["origin_issue"] == 4
