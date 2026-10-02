"""Azure-DevOps-spezifische Tests (argv, HTML-Roundtrip, Policies, Registry, Templates)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from az_sim import AzSim
from forge_adapters.azure import (
    AzureBoardsTracker,
    AzureReposCodeHost,
    html_to_text,
    summarize_policies,
    text_to_html,
)
from forge_adapters.azure.templates import list_templates
from forge_core.spec import AzureDevOpsConfig, CostCapsConfig, ProjectSpec, SpecValidationError
from forge_core.tracking import NewWorkItem

CFG = AzureDevOpsConfig(organization="contoso", project="Shop", repository="web")


def test_org_url_normalisation() -> None:
    assert CFG.org_url == "https://dev.azure.com/contoso"
    assert AzureDevOpsConfig(organization="https://dev.azure.com/x/", project="p").org_url == (
        "https://dev.azure.com/x"
    )
    assert AzureDevOpsConfig(organization="o", project="p").repo_name == "p"


def test_html_roundtrip_keeps_parseable_lines() -> None:
    body = "Depends-On: #3, #4\nTouches: src/a/**\n\n<b>x</b> & y"
    assert html_to_text(text_to_html(body)) == body
    assert html_to_text("<div>A</div><div>B<br/>C</div>") == "A\nB\nC"


def test_policy_summary_ignores_reviewer_policies() -> None:
    build = {"configuration": {"type": {"displayName": "Build"}}}
    reviewers = {"status": "rejected",
                 "configuration": {"type": {"displayName": "Minimum number of reviewers"}}}
    assert summarize_policies([reviewers]) == "none"
    assert summarize_policies([{**build, "status": "approved"}, reviewers]) == "pass"
    assert summarize_policies([{**build, "status": "running"}]) == "pending"
    assert summarize_policies([{**build, "status": "rejected"}]) == "fail"


def test_set_stage_rewrites_tags_and_is_noop_when_unchanged() -> None:
    sim = AzSim()
    sim.add_item(7, "x", labels=["bug", "forge:ready"])
    tracker = AzureBoardsTracker(config=CFG, run_subprocess=sim)
    tracker.set_stage(number=7, add="forge:in-dev", remove="forge:ready")
    assert sim.items[7]["fields"]["System.Tags"] == "bug; forge:in-dev"
    updates = [c for c in sim.calls if c[1:4] == ["boards", "work-item", "update"]]
    tracker.set_stage(number=7, add="forge:in-dev", remove="forge:ready")
    assert len([c for c in sim.calls if c[1:4] == ["boards", "work-item", "update"]]) == len(
        updates
    )


def test_create_item_maps_kind_to_work_item_type_and_links_parent() -> None:
    sim = AzSim()
    sim.add_item(1, "Epic", wit="Epic")
    tracker = AzureBoardsTracker(config=CFG, run_subprocess=sim)
    created = tracker.create_item(
        NewWorkItem(kind="story", title="Login", body="AC 1", labels=["forge:proposed"], parent=1)
    )
    create = next(c for c in sim.calls if c[1:4] == ["boards", "work-item", "create"])
    assert create[create.index("--type") + 1] == "User Story"
    assert any(c[1:5] == ["boards", "work-item", "relation", "add"] for c in sim.calls)
    assert created.kind == "story"
    assert tracker.get_items([created.number])[0].kind == "story"


def test_every_az_call_disables_detection_and_names_org() -> None:
    sim = AzSim()
    sim.add_item(1, "x", labels=["forge:ready"])
    tracker = AzureBoardsTracker(config=CFG, run_subprocess=sim)
    tracker.list_stage_items(stage_labels=["forge:ready"])
    tracker.comment(number=1, body="hi")
    for call in sim.calls:
        assert call[call.index("--org") + 1] == "https://dev.azure.com/contoso"
        assert call[call.index("--detect") + 1] == "false"


def test_wiql_quotes_are_escaped() -> None:
    sim = AzSim()
    tracker = AzureBoardsTracker(config=CFG, run_subprocess=sim)
    tracker.list_stage_items(stage_labels=["it's"])
    wiql = sim.calls[-1][sim.calls[-1].index("--wiql") + 1]
    assert "'it''s'" in wiql


def test_open_change_truncates_description_and_passes_labels(tmp_path: Path) -> None:
    sim = AzSim()
    host = AzureReposCodeHost(config=CFG, repo_root=tmp_path, run_subprocess=sim)
    pr = host.open_change(branch="forge/r1", title="t", body="x" * 5000, labels=["forge:auto"])
    create = next(c for c in sim.calls if c[1:4] == ["repos", "pr", "create"])
    assert len(create[create.index("--description") + 1]) <= 4000
    assert create[create.index("--labels") + 1] == "forge:auto"
    assert create[create.index("--repository") + 1] == "web"
    assert pr.url.endswith(f"/_git/web/pullrequest/{pr.pr_number}")


def test_post_review_votes_and_posts_thread(tmp_path: Path) -> None:
    sim = AzSim()
    host = AzureReposCodeHost(config=CFG, repo_root=tmp_path, run_subprocess=sim)
    pr = host.open_change(branch="forge/r1", title="t", body="b")
    host.post_review(pr_number=pr.pr_number, approve=False, body="please fix")
    assert sim.prs[pr.pr_number]["votes"] == ["wait-for-author"]
    assert sim.threads and sim.threads[0][0] == pr.pr_number


def test_release_is_annotated_tag(tmp_path: Path) -> None:
    sim = AzSim()
    host = AzureReposCodeHost(config=CFG, repo_root=tmp_path, run_subprocess=sim)
    url = host.create_release(tag="v1.0.0", title="v1.0.0", notes="changes")
    assert "refs/tags/v1.0.0" in sim.pushed
    assert url.endswith("?version=GTv1.0.0")
    assert any(c[:3] == ["git", "tag", "-a"] for c in sim.calls)


def _spec(**provider) -> ProjectSpec:
    return ProjectSpec(
        spec_version="1.0", name="p",
        cost_caps=CostCapsConfig(per_generation_usd=Decimal(1), per_run_usd=Decimal(1),
                                 per_project_per_day_usd=Decimal(1),
                                 per_project_per_month_usd=Decimal(1)),
        provider=provider,
    )


def test_spec_requires_azure_block() -> None:
    with pytest.raises((SpecValidationError, ValueError)):
        _spec(tracker="azure_devops")
    spec = _spec(tracker="azure_devops", azure={"organization": "o", "project": "p"})
    assert spec.provider.effective_code_host == "azure_devops"


def test_spec_allows_azure_board_without_github_fields() -> None:
    spec = ProjectSpec.model_validate({
        "spec_version": "1.0", "name": "p",
        "cost_caps": {"per_generation_usd": 1, "per_run_usd": 1,
                      "per_project_per_day_usd": 1, "per_project_per_month_usd": 1},
        "provider": {"tracker": "azure_devops", "azure": {"organization": "o", "project": "p"}},
        "board": {"filter_status": "New", "filter_labels": ["forge"]},
    })
    assert spec.board is not None and spec.board.owner is None


def test_spec_rejects_github_board_without_owner() -> None:
    with pytest.raises((SpecValidationError, ValueError)):
        ProjectSpec.model_validate({
            "spec_version": "1.0", "name": "p",
            "cost_caps": {"per_generation_usd": 1, "per_run_usd": 1,
                          "per_project_per_day_usd": 1, "per_project_per_month_usd": 1},
            "board": {"filter_status": "Todo"},
        })


def test_registry_builds_mixed_azure_boards_with_github_repos(tmp_path: Path) -> None:
    from forge_adapters.github import GitHubCodeHost
    from forge_adapters.registry import build_code_host, build_tracker

    spec = _spec(tracker="azure_devops", code_host="github",
                 azure={"organization": "o", "project": "p"})
    assert isinstance(build_tracker(spec, tmp_path), AzureBoardsTracker)
    assert isinstance(build_code_host(spec, tmp_path), GitHubCodeHost)


def test_azure_pipeline_templates_are_valid_yaml() -> None:
    names = {p.name for p in list_templates()}
    assert {"forge-conductor.yml", "forge-pr-review.yml"} <= names
    for tpl in list_templates():
        data = yaml.safe_load(tpl.read_text(encoding="utf-8"))
        assert "jobs" in data
        assert "System.AccessToken" in tpl.read_text(encoding="utf-8")


def test_example_azure_spec_loads() -> None:
    from forge_core.spec import load_spec

    root = Path(__file__).resolve().parents[3]
    spec = load_spec(root / "examples" / "azure-devops" / ".forge" / "project.yaml")
    assert spec.provider.tracker == "azure_devops"
    assert spec.provider.azure is not None and spec.provider.azure.repo_name == "shop-web"
