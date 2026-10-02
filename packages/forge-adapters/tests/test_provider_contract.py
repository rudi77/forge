"""Vertrags-Testsuite für ``WorkTracker``/``CodeHost``.

Jede Implementierung (In-Memory, GitHub, Azure DevOps, …) muss dieselben
Semantik-Tests bestehen. Die echten Adapter laufen gegen CLI-Simulatoren
(``gh_sim``/``az_sim``) — geprüft wird damit argv-Bau + JSON-Mapping, ohne Netz.
Ein neuer Anbieter ist fertig, wenn er hier als Parameter grün läuft.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest
from forge_adapters.base import CodeHost, CodeHostError, TrackerError, WorkTracker
from forge_adapters.fake import InMemoryCodeHost, InMemoryTracker
from forge_adapters.github import GitHubCodeHost, GitHubTracker
from forge_core.tracking import NewWorkItem, ReadyIssue
from gh_sim import GhSim


@dataclass
class Provider:
    tracker: WorkTracker
    code_host: CodeHost
    seed: Callable[..., None]
    """seed(number, title, body, labels, open_=True) legt ein Item an."""


def _memory(_tmp: Path) -> Provider:
    tracker = InMemoryTracker()

    def seed(number, title, body="", labels=(), open_=True):
        tracker.add(
            ReadyIssue(
                number=number, title=title, body=body, labels=list(labels),
                project_status="", url=f"fake://{number}",
            ),
            open_=open_,
        )

    return Provider(tracker, InMemoryCodeHost(), seed)


def _github(tmp: Path) -> Provider:
    sim = GhSim()
    return Provider(
        GitHubTracker(owner="o", repo="r", run_subprocess=sim),
        GitHubCodeHost(repo_root=tmp, run_subprocess=sim),
        sim.add_issue,
    )


def _azure(tmp: Path) -> Provider:
    from az_sim import AzSim
    from forge_adapters.azure import AzureBoardsTracker, AzureReposCodeHost
    from forge_core.spec import AzureDevOpsConfig

    sim = AzSim()
    cfg = AzureDevOpsConfig(organization="org", project="proj")
    return Provider(
        AzureBoardsTracker(config=cfg, run_subprocess=sim),
        AzureReposCodeHost(config=cfg, repo_root=tmp, run_subprocess=sim),
        sim.add_item,
    )


PROVIDERS = {"memory": _memory, "github": _github, "azure": _azure}


@pytest.fixture(params=sorted(PROVIDERS))
def provider(request, tmp_path: Path) -> Provider:
    return PROVIDERS[request.param](tmp_path)


# --- Struktur ---------------------------------------------------------------


def _public_methods(cls) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(fn)
        for name, fn in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def _azure_classes():
    from forge_adapters.azure import AzureBoardsTracker, AzureReposCodeHost

    return AzureBoardsTracker, AzureReposCodeHost


@pytest.mark.parametrize("impl", [InMemoryTracker, GitHubTracker, _azure_classes()[0]])
def test_tracker_implements_full_protocol(impl) -> None:
    proto = _public_methods(WorkTracker)
    have = _public_methods(impl)
    for name, sig in proto.items():
        assert name in have, f"{impl.__name__} lacks {name}"
        assert list(have[name].parameters) == list(sig.parameters), name


@pytest.mark.parametrize("impl", [InMemoryCodeHost, GitHubCodeHost, _azure_classes()[1]])
def test_code_host_implements_full_protocol(impl) -> None:
    proto = _public_methods(CodeHost)
    have = _public_methods(impl)
    for name, sig in proto.items():
        assert name in have, f"{impl.__name__} lacks {name}"
        assert list(have[name].parameters) == list(sig.parameters), name


# --- WorkTracker-Semantik ------------------------------------------------------


def test_list_stage_items_filters_and_sorts(provider: Provider) -> None:
    provider.seed(3, "c", labels=["forge:ready"])
    provider.seed(1, "a", labels=["forge:design", "bug"])
    provider.seed(2, "b", labels=["bug"])
    provider.seed(4, "d", labels=["forge:done"], open_=False)

    open_items = provider.tracker.list_stage_items(
        stage_labels=["forge:design", "forge:ready", "forge:done"]
    )
    assert [i.number for i in open_items] == [1, 3]
    all_items = provider.tracker.list_stage_items(
        stage_labels=["forge:design", "forge:ready", "forge:done"], state="all"
    )
    assert [i.number for i in all_items] == [1, 3, 4]
    assert "forge:design" in all_items[0].labels


def test_set_stage_moves_label_and_is_idempotent(provider: Provider) -> None:
    provider.seed(5, "x", labels=["forge:ready"])
    provider.tracker.set_stage(number=5, add="forge:in-dev", remove="forge:ready")
    provider.tracker.set_stage(number=5, add="forge:in-dev", remove="forge:ready")
    (item,) = provider.tracker.get_items([5])
    assert "forge:in-dev" in item.labels
    assert "forge:ready" not in item.labels
    assert item.labels.count("forge:in-dev") == 1


def test_comment_and_close(provider: Provider) -> None:
    provider.seed(7, "x", labels=["forge:qa"])
    provider.tracker.comment(number=7, body="hello")
    provider.tracker.close(number=7, reason="completed")
    assert provider.tracker.list_stage_items(stage_labels=["forge:qa"]) == []
    assert [i.number for i in provider.tracker.list_stage_items(
        stage_labels=["forge:qa"], state="all"
    )] == [7]


def test_get_items_unknown_raises(provider: Provider) -> None:
    with pytest.raises(TrackerError):
        provider.tracker.get_items([999])


def test_ensure_labels_reports_and_creates(provider: Provider) -> None:
    provider.seed(1, "x", labels=["forge:ready"])
    report = provider.tracker.ensure_labels(["forge:ready", "forge:qa"], create=True)
    assert "forge:qa" not in report.missing
    again = provider.tracker.ensure_labels(["forge:ready", "forge:qa"])
    assert again.missing == []


def test_create_item_then_find_by_fingerprint(provider: Provider) -> None:
    provider.seed(1, "epic", labels=["forge:epic"])
    fp = "forge-fingerprint: sha256:" + "a" * 64
    created = provider.tracker.create_item(
        NewWorkItem(
            kind="bug",
            title="Null pointer in parser",
            body=f"Steps…\n\n{fp}",
            labels=["forge:proposed", "forge:generated"],
            parent=1,
        )
    )
    assert created.number > 0
    assert created.kind == "bug"
    (loaded,) = provider.tracker.get_items([created.number])
    assert "forge:proposed" in loaded.labels
    found = provider.tracker.search_items(fp)
    assert [i.number for i in found] == [created.number]
    assert provider.tracker.search_items("forge-fingerprint: sha256:" + "b" * 64) == []


# --- CodeHost-Semantik -----------------------------------------------------------


def test_open_review_merge_lifecycle(provider: Provider) -> None:
    host = provider.code_host
    pr = host.open_change(branch="forge/r1", title="t", body="b", labels=["forge:auto"])
    assert pr.pr_number > 0
    meta = host.fetch_metadata(pr.pr_number)
    assert meta.state == "OPEN"
    assert meta.head_branch == "forge/r1"
    assert meta.ci_status in {"pass", "fail", "pending", "none", "unknown"}
    assert [c.number for c in host.list_open_changes()] == [pr.pr_number]
    assert isinstance(host.fetch_diff(pr.pr_number), str)
    ts = host.head_committed_at(pr.pr_number)
    assert ts is None or isinstance(ts, datetime)

    host.post_review(pr_number=pr.pr_number, approve=False, body="please fix")
    result = host.merge(pr_number=pr.pr_number)
    assert result.merged
    assert host.fetch_metadata(pr.pr_number).state != "OPEN"
    assert host.list_open_changes() == []
    with pytest.raises(CodeHostError):
        host.merge(pr_number=pr.pr_number)


def test_create_release_is_idempotent(provider: Provider) -> None:
    first = provider.code_host.create_release(tag="v1.2.0", title="v1.2.0", notes="x")
    second = provider.code_host.create_release(tag="v1.2.0", title="v1.2.0", notes="x")
    assert isinstance(first, str)
    assert first == second


def test_push_branch_never_forces(provider: Provider) -> None:
    provider.code_host.push_branch(branch="forge/abc")
    # Struktur-Garantie für Sim-basierte Provider: kein --force im argv.
    calls = getattr(getattr(provider.code_host, "_run", None), "calls", [])
    assert all("--force" not in c and "-f" not in c for c in calls)


def test_push_onto_existing_branch_only_for_forge_branches(provider: Provider) -> None:
    provider.code_host.push_branch(branch="forge/new", target="forge/pr-head")
    with pytest.raises(CodeHostError, match="non-forge"):
        provider.code_host.push_branch(branch="forge/new", target="main")
    with pytest.raises(CodeHostError):
        provider.code_host.push_branch(branch="forge/new", target="feature/human")


def test_ci_helpers_fail_open(provider: Provider) -> None:
    assert provider.code_host.ci_failure_summary(424242) == ""
    assert provider.code_host.ref_ci_status("main") in {
        "pass", "fail", "pending", "none", "unknown",
    }
