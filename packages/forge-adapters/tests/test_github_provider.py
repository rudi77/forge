"""GitHub-spezifische Tests für ``GitHubTracker``/``GitHubCodeHost`` (argv + Parsing)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from forge_adapters.base import CodeHostError, TrackerError
from forge_adapters.github import GitHubCodeHost, GitHubTracker
from forge_core.tracking import NewWorkItem
from gh_sim import GhSim


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], rc, out, err)


def test_comment_argv() -> None:
    run = MagicMock(return_value=_cp())
    GitHubTracker(owner="o", repo="r", run_subprocess=run).comment(number=7, body="hi")
    argv = run.call_args.args[0]
    assert argv[:4] == ["gh", "issue", "comment", "7"]
    assert argv[argv.index("--repo") + 1] == "o/r"
    assert argv[argv.index("--body") + 1] == "hi"


def test_close_passes_reason() -> None:
    run = MagicMock(return_value=_cp())
    GitHubTracker(owner="o", repo="r", run_subprocess=run).close(number=3, reason="completed")
    argv = run.call_args.args[0]
    assert argv[argv.index("--reason") + 1] == "completed"


def test_tracker_errors_are_tracker_errors() -> None:
    run = MagicMock(return_value=_cp(1, err="no auth"))
    with pytest.raises(TrackerError, match="no auth"):
        GitHubTracker(owner="o", repo="r", run_subprocess=run).comment(number=1, body="x")


def test_ensure_labels_without_create_only_reports() -> None:
    sim = GhSim()
    sim.labels.add("forge:ready")
    report = GitHubTracker(owner="o", repo="r", run_subprocess=sim).ensure_labels(
        ["forge:ready", "forge:qa"]
    )
    assert report.present == ["forge:ready"]
    assert report.missing == ["forge:qa"]
    assert not any(c[1:3] == ["label", "create"] for c in sim.calls)


def test_create_item_adds_type_label_parent_line_and_links_sub_issue() -> None:
    sim = GhSim()
    sim.add_issue(10, "epic", labels=["forge:epic"])
    item = GitHubTracker(owner="o", repo="r", run_subprocess=sim).create_item(
        NewWorkItem(kind="story", title="Login", body="AC", labels=["forge:proposed"], parent=10)
    )
    assert "type:story" in item.labels
    assert "Parent: #10" in item.body
    assert any("/sub_issues" in " ".join(c) for c in sim.calls)


def test_ci_failure_summary_lists_failed_checks_and_log_tail(tmp_path: Path) -> None:
    rollup = {
        "statusCheckRollup": [
            {"name": "lint", "status": "COMPLETED", "conclusion": "SUCCESS"},
            {
                "name": "tests",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/o/r/actions/runs/555/job/1",
            },
        ]
    }

    def run(argv, **_kw):
        if argv[1:3] == ["pr", "view"]:
            return _cp(out=json.dumps(rollup))
        if argv[1:3] == ["run", "view"]:
            assert argv[3] == "555"
            return _cp(out="x" * 50 + "\nAssertionError: boom\n")
        return _cp(1)

    host = GitHubCodeHost(repo_root=tmp_path, run_subprocess=run)
    summary = host.ci_failure_summary(7)
    assert "tests" in summary
    assert "lint" not in summary
    assert "AssertionError: boom" in summary


def test_ci_failure_summary_green_is_empty(tmp_path: Path) -> None:
    sim = GhSim()
    host = GitHubCodeHost(repo_root=tmp_path, run_subprocess=sim)
    pr = host.open_change(branch="forge/x", title="t", body="b")
    assert host.ci_failure_summary(pr.pr_number) == ""


def test_open_change_does_not_emit_events_and_pushes(tmp_path: Path) -> None:
    sim = GhSim()
    host = GitHubCodeHost(repo_root=tmp_path, run_subprocess=sim)
    pr = host.open_change(branch="forge/x", title="t", body="b", labels=["a", "b"])
    assert sim.pushed == ["forge/x"]
    create = next(c for c in sim.calls if c[1:3] == ["pr", "create"])
    assert create.count("--label") == 2
    assert pr.url.endswith(f"/pull/{pr.pr_number}")


def test_open_change_error_is_code_host_error(tmp_path: Path) -> None:
    run = MagicMock(return_value=_cp(1, err="denied"))
    with pytest.raises(CodeHostError, match="denied"):
        GitHubCodeHost(repo_root=tmp_path, run_subprocess=run).open_change(
            branch="b", title="t", body="x", push=False
        )


def test_registry_builds_github_from_remote(tmp_path: Path) -> None:
    from forge_adapters.registry import build_code_host, build_tracker
    from forge_core.spec import CostCapsConfig, ProjectSpec

    spec = ProjectSpec(
        spec_version="1.0",
        name="p",
        cost_caps=CostCapsConfig(
            per_generation_usd=1, per_run_usd=1, per_project_per_day_usd=1,
            per_project_per_month_usd=1,
        ),
    )
    run = MagicMock(return_value=_cp(out="git@github.com:acme/widget.git\n"))
    tracker = build_tracker(spec, tmp_path, run_subprocess=run)
    assert isinstance(tracker, GitHubTracker)
    assert tracker.slug == "acme/widget"
    assert isinstance(build_code_host(spec, tmp_path), GitHubCodeHost)
