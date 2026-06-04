"""Tests für die Roster-Auflösung aus der Trigger-Config im board-loop.

``_roster_for_issue`` macht die ``agents:[...]``-Spec-Config funktional: das
erste konfigurierte Issue-Label bestimmt, welche Arbeitspferde mitwirken.
"""

from __future__ import annotations

from decimal import Decimal

from forge_cli.board_loop import _roster_for_issue
from forge_core.spec import (
    CapabilitiesConfig,
    CostCapsConfig,
    IssueLabelTriggerConfig,
    ProjectSpec,
    TriggersConfig,
)


def _spec(**triggers) -> ProjectSpec:
    return ProjectSpec(
        spec_version="1.0",
        name="testproj",
        cost_caps=CostCapsConfig(
            per_generation_usd=Decimal("0.5"),
            per_run_usd=Decimal("2.0"),
            per_project_per_day_usd=Decimal("10.0"),
            per_project_per_month_usd=Decimal("50.0"),
        ),
        capabilities=CapabilitiesConfig(),
        triggers=TriggersConfig(**triggers),
    )


def test_roster_from_matching_label() -> None:
    spec = _spec(
        on_issue_label={
            "auto-fix": IssueLabelTriggerConfig(
                strategy="sequential", model="sonnet", agents=["developer"]
            ),
            "auto-feature": IssueLabelTriggerConfig(
                strategy="sequential",
                model="opus",
                agents=["architect", "developer", "tester"],
            ),
        }
    )
    assert _roster_for_issue(spec, ["auto-fix"]) == ["developer"]
    assert _roster_for_issue(spec, ["auto-feature"]) == [
        "architect",
        "developer",
        "tester",
    ]


def test_roster_first_matching_label_wins() -> None:
    spec = _spec(
        on_issue_label={
            "auto-fix": IssueLabelTriggerConfig(
                strategy="sequential", model="sonnet", agents=["developer"]
            ),
        }
    )
    # "bug" matcht nicht, "auto-fix" schon → dessen Roster.
    assert _roster_for_issue(spec, ["bug", "auto-fix"]) == ["developer"]


def test_roster_none_when_no_label_matches() -> None:
    spec = _spec(
        on_issue_label={
            "auto-fix": IssueLabelTriggerConfig(
                strategy="sequential", model="sonnet", agents=["developer"]
            ),
        }
    )
    assert _roster_for_issue(spec, ["bug", "wontfix"]) is None


def test_roster_none_when_no_triggers() -> None:
    spec = _spec()
    assert _roster_for_issue(spec, ["bug"]) is None
