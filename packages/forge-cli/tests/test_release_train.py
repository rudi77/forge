"""L3: Release-Train — SemVer, Changelog, Versionsdateien, Train-Ablauf."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_cli import board_loop as bl
from forge_cli.conductor import WorkItem, derive_signals, plan_tick
from forge_cli.release import (
    RELEASE_PR_LABEL,
    Commit,
    SemVer,
    bump_kind,
    bump_version_file,
    collect_commits,
    latest_version,
    next_version,
    parse_release_items,
    prepend_changelog,
    render_changelog,
    train_tick,
)
from forge_cli.runtime import ForgeContext
from forge_cli.stages import Stage, StageSignals
from forge_core.events import EventKind as EK
from forge_core.spec import (
    BoardConfig,
    CapabilitiesConfig,
    CostCapsConfig,
    ProjectSpec,
    ReleaseConfig,
    TriageConfig,
)
from forge_core.tracking import ReadyIssue

# --- rein -----------------------------------------------------------------------------


def test_semver_parse_and_latest() -> None:
    assert SemVer.parse("v1.2.3", "v") == SemVer(1, 2, 3)
    assert SemVer.parse("1.2", "") is None
    assert latest_version(["v0.9.0", "v0.10.1", "forge-issue-3", "v0.10.0"]) == SemVer(0, 10, 1)


@pytest.mark.parametrize(
    "subject,body,kind",
    [
        ("feat(auth): login", "", "minor"),
        ("fix: crash", "", "patch"),
        ("perf: faster", "", "patch"),
        ("feat!: new api", "", "major"),
        ("refactor: x", "BREAKING CHANGE: removed y", "major"),
        ("docs: readme", "", None),
        ("random message", "", None),
    ],
)
def test_bump_kind(subject, body, kind) -> None:
    assert bump_kind(Commit(subject, body)) == kind


def test_next_version_rules() -> None:
    feat, fix, brk = Commit("feat: a"), Commit("fix: b"), Commit("feat!: c")
    assert next_version(SemVer(1, 2, 3), [fix]) == SemVer(1, 2, 4)
    assert next_version(SemVer(1, 2, 3), [fix, feat]) == SemVer(1, 3, 0)
    assert next_version(SemVer(1, 2, 3), [brk]) == SemVer(2, 0, 0)
    assert next_version(SemVer(0, 4, 1), [brk]) == SemVer(0, 5, 0)  # 0.x: breaking → minor
    assert next_version(None, [fix]) == SemVer(0, 1, 0)
    assert next_version(SemVer(1, 0, 0), [Commit("chore: x")]) == SemVer(1, 0, 1)
    assert next_version(SemVer(1, 0, 0), [feat], conventional=False) == SemVer(1, 0, 1)


def test_render_and_prepend_changelog() -> None:
    commits = [
        Commit("feat(auth): login with SSO"),
        Commit("fix: crash on empty input"),
        Commit("chore(release): v0.1.0"),
        Commit("tidy up"),
    ]
    section = render_changelog(SemVer(0, 2, 0), commits, day=date(2026, 6, 1), issues=[4, 3])
    assert section.startswith("## [0.2.0] — 2026-06-01")
    assert "### Features\n\n- **auth:** login with SSO" in section
    assert "### Fixes\n\n- crash on empty input" in section
    assert "### Other changes\n\n- tidy up" in section
    assert "chore(release)" not in section
    assert "Work items: #3, #4" in section
    merged = prepend_changelog("# Changelog\n\nintro\n\n## [0.1.0] — x\n- a\n", section)
    assert merged.index("## [0.2.0]") < merged.index("## [0.1.0]")
    assert merged.startswith("# Changelog")
    assert prepend_changelog("", section).startswith("# Changelog\n\n## [0.2.0]")


def test_bump_version_files() -> None:
    v = SemVer(1, 4, 0)
    toml = '[project]\nname = "x"\nversion = "1.3.2"\n[tool.y]\nversion = "9"\n'
    assert 'version = "1.4.0"' in bump_version_file("pyproject.toml", toml, v)
    assert 'version = "9"' in bump_version_file("pyproject.toml", toml, v)
    pkg = bump_version_file("web/package.json", '{"name": "x", "version": "1.0.0"}', v)
    assert json.loads(pkg)["version"] == "1.4.0"
    assert bump_version_file("VERSION", "1.0.0\n", v) == "1.4.0\n"
    assert bump_version_file("setup.cfg", "version=1", v) == "version=1"


def test_parse_release_items() -> None:
    assert parse_release_items("notes\nRelease-Items: #3, #12\n") == [3, 12]
    assert parse_release_items("no items") == []


def test_release_items_are_not_dispatched_in_train_mode() -> None:
    item = WorkItem(3, Stage.RELEASE, signals=StageSignals(release_batched=True))
    assert plan_tick([item], capacity=2).dispatch == []


# --- git-basiert ----------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
                          cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-q", str(remote))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    (seed / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.0.0"\n')
    _git(seed, "add", ".")
    _git(seed, "commit", "-q", "-m", "chore: init")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "feat(api): add search")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "fix: handle empty query")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(remote), str(clone))
    return clone


def test_collect_commits_since_tag(repo: Path) -> None:
    commits = collect_commits(repo, base_ref="origin/main", since_tag=None)
    assert [c.subject for c in commits][:2] == ["fix: handle empty query", "feat(api): add search"]
    _git(repo, "tag", "v0.1.0", "origin/main~1")
    commits = collect_commits(repo, base_ref="origin/main", since_tag="v0.1.0")
    assert [c.subject for c in commits] == ["fix: handle empty query"]


def _ctx(repo: Path, **release) -> ForgeContext:
    spec = ProjectSpec(
        spec_version="1.0", name="p",
        cost_caps=CostCapsConfig(per_generation_usd=Decimal("1"), per_run_usd=Decimal("1"),
                                 per_project_per_day_usd=Decimal("10"),
                                 per_project_per_month_usd=Decimal("10")),
        board=BoardConfig(owner="x", project_number=1), triage=TriageConfig(enabled=False),
        capabilities=CapabilitiesConfig(create_release=True),
        release=ReleaseConfig(mode="train", version_files=["pyproject.toml"], **release),
    )
    return ForgeContext(
        repo_root=repo, forge_dir=repo / ".forge", spec=spec,
        spec_path=repo / ".forge" / "p.yaml", factory_version="git:t",
        project_fingerprint="sha256:t", store_path=repo / ".forge" / "e.duckdb",
        blobs_path=repo / ".forge" / "blobs", tracker=InMemoryTracker(),
        code_host=InMemoryCodeHost(),
    )


def _tick(ctx: ForgeContext, waiting: list[int], **kw):
    store = ctx.open_store()
    try:
        events = []
        for kind in (EK.PR_CREATED, EK.PR_MERGED, EK.PR_REVIEWED, EK.RELEASE_TAGGED,
                     EK.RUN_STARTED):
            events.extend(store.events_by_kind(kind))
        return train_tick(ctx, store=store, session_id="s", events=events, waiting_items=waiting,
                          base="main", now=datetime(2026, 6, 1, 12, tzinfo=UTC), **kw)
    finally:
        store.close()


def test_train_prepares_reviews_and_tags(repo: Path) -> None:
    ctx = _ctx(repo)
    res = _tick(ctx, [3, 4])
    assert (res.action, res.detail) == ("prepared", "v0.1.0")
    (pr,) = ctx.code_host.prs.values()
    assert RELEASE_PR_LABEL in pr.labels
    assert pr.title == "chore(release): v0.1.0"
    assert pr.branch.startswith("forge/release-0.1.0-")
    assert "### Features" in pr.body and "Release-Items: #3, #4" in pr.body
    assert ctx.code_host.pushed[0].endswith(f":{pr.branch}")

    reviewed: list[int] = []
    assert _tick(ctx, [3, 4], review_fn=reviewed.append).action == "reviewed"
    assert reviewed == [pr.number]

    pr.state = "MERGED"
    res = _tick(ctx, [3, 4])
    assert (res.action, res.detail) == ("tagged", "v0.1.0")
    _title, notes = ctx.code_host.releases["v0.1.0"]
    assert "### Fixes" in notes and "Release-Items" not in notes

    store = ctx.open_store()
    (tag,) = store.events_by_kind(EK.RELEASE_TAGGED)
    store.close()
    assert tag.payload["issue_numbers"] == [3, 4] and tag.payload["version"] == "0.1.0"

    # Danach ist der Train wieder leer → nächstes Release erst mit neuen Items.
    assert _tick(ctx, []).action == "idle"

    class _E:
        def __init__(self, kind, run_id, payload):
            self.kind, self.run_id, self.payload = kind, run_id, payload
            self.ts = datetime(2026, 6, 1, tzinfo=UTC)

    started = _E(EK.RUN_STARTED, "r4", {"issue_number": 4})
    assert derive_signals([started, tag], 4).release_done


def test_train_respects_min_items_capability_and_closed_prs(repo: Path) -> None:
    ctx = _ctx(repo, min_items=2)
    assert _tick(ctx, [3]).action == "idle"
    ctx.spec.capabilities.create_release = False
    assert _tick(ctx, [3, 4]).action == "idle"
    ctx.spec.capabilities.create_release = True
    assert _tick(ctx, [3, 4]).action == "prepared"
    (pr,) = ctx.code_host.prs.values()
    pr.state = "CLOSED"  # ein Mensch hat den Release-PR verworfen
    assert _tick(ctx, [3, 4]).action == "prepared"
    assert len(ctx.code_host.prs) == 2


def test_train_schedule_gates_preparation(repo: Path) -> None:
    ctx = _ctx(repo, schedule="0 9 * * 1")  # montags 09:00
    assert _tick(ctx, [3]).action == "idle"  # 2026-06-01 12:00 ist kein Treffer


def test_conductor_runs_train_instead_of_per_item_release(
    repo: Path, monkeypatch: Any
) -> None:
    ctx = _ctx(repo)
    ctx.tracker.add(ReadyIssue(3, "a", "", ["forge:release"], "", ""))
    monkeypatch.setattr(bl, "_dispatch_release_run", lambda **kw: (_ for _ in ()).throw(
        AssertionError("no per-item release in train mode")))
    params = bl._DispatchParams(
        template_id="t", focus_template="issue-{number}", base_ref="HEAD", max_iterations=1,
        max_turns=4, eval_suite="quick", model=None, claude_bin="claude", multi_agent=False,
        auto_merge=False, pr_base="main", pr_label=None,
    )
    bl._run_conductor_watch(ctx=ctx, max_issues=3, interval_s=0, params=params,
                            triager=None, capabilities=None, max_ticks=1)
    (pr,) = ctx.code_host.prs.values()
    assert "Release-Items: #3" in pr.body


def test_factory_releases_view_counts_items_and_lead_time(tmp_path: Path) -> None:
    from forge_cli.analyze import _section_releases
    from forge_core.events import ReleaseTaggedPayload, RunStartedPayload, build_event
    from forge_core.store import EventStore

    store = EventStore(tmp_path / "e.duckdb")
    common = dict(project="p", project_fingerprint="sha256:t", factory_version="git:t",
                  spec_version="1.0")
    for n in (3, 4):
        store.append(build_event(kind=EK.RUN_STARTED, run_id=f"r{n}", payload=RunStartedPayload(
            trigger="issue_label", strategy="sequential", config_hash="c", issue_number=n),
            **common))
    store.append(build_event(kind=EK.RELEASE_TAGGED, run_id="s", payload=ReleaseTaggedPayload(
        issue_number=3, tag="v0.1.0", version="0.1.0", issue_numbers=[3, 4]), **common))
    store.append(build_event(kind=EK.RELEASE_TAGGED, run_id="s", payload=ReleaseTaggedPayload(
        issue_number=5, tag="forge/epic-1", integrated_into="forge/epic-1"), **common))
    rows = store.query("SELECT tag, items, mean_lead_time_h FROM factory_releases")
    assert [(r["tag"], r["items"]) for r in rows] == [("v0.1.0", 2)]
    assert rows[0]["mean_lead_time_h"] is not None
    assert "v0.1.0" in _section_releases(store)
    store.close()
