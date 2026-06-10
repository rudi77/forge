"""Projekt-Cost-Caps (Spec Teil 5.3): `project_day`/`project_month` blockieren
einen Run, BEVOR Worktree oder Agent angefasst werden."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from forge_core.blobs import BlobStore
from forge_core.events import EventKind, RunFinishedPayload, build_event
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from forge_execute.runner import RunConfig, SequentialRunner

COMMON = dict(
    project="capped",
    project_fingerprint="sha256:test",
    factory_version="git:test",
    spec_version="1.0",
)

_FIXED_NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


class _FixedDatetime:
    """Ersatz für `datetime` im Runner-Modul: deterministisches `now`."""

    @classmethod
    def now(cls, tz=None):
        return _FIXED_NOW


class _MustNotBeCalledAgent:
    """Agent-Stub: jeder Aufruf ist ein Testfehler — bei ausgeschöpftem
    Projekt-Budget darf kein LLM-Call passieren."""

    def propose(self, **kwargs):
        raise AssertionError("propose() darf bei Projekt-Cap nicht laufen")

    def review(self, **kwargs):
        raise AssertionError("review() darf bei Projekt-Cap nicht laufen")


def _spec() -> ProjectSpec:
    return ProjectSpec.model_validate(
        {
            "spec_version": "1.0",
            "name": "capped",
            "cost_caps": {
                "per_generation_usd": "0.50",
                "per_run_usd": "1.00",
                "per_project_per_day_usd": "2.00",
                "per_project_per_month_usd": "10.00",
            },
            "surfaces": {"code": {"paths": ["src/"], "type": "code"}},
            "forbidden": [],
            "capabilities": {"run": ["pytest *"]},
            "eval_suites": {
                "quick": {
                    "cmd": f'"{sys.executable}" -m pytest -q',
                    "budget_s": 60,
                    "parses": "pytest_json",
                },
            },
            "gates": [
                {"kind": "pytest_pass_rate", "threshold": 1.0, "source": "quick"},
            ],
        }
    )


def _seed_finished_run(
    store: EventStore, *, run_id: str, cost: str, ts: datetime
) -> None:
    store.append(
        build_event(
            kind=EventKind.RUN_FINISHED,
            run_id=run_id,
            payload=RunFinishedPayload(
                decision="pr_created",
                generations_count=1,
                total_cost_usd=Decimal(cost),
            ),
            ts=ts,
            **COMMON,
        )
    )


def _make_runner(tmp_path: Path, store: EventStore) -> SequentialRunner:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    config = RunConfig(
        spec=_spec(),
        project="capped",
        project_fingerprint="sha256:test",
        factory_version="git:test",
        repo_root=repo_root,
        prompt_template_id="fix_failing_test_v1",
        initial_prompt="irrelevant",
        max_iterations=1,
    )
    blobs = BlobStore(tmp_path / "blobs")
    return SequentialRunner(
        config=config, agent=_MustNotBeCalledAgent(), store=store, blobs=blobs
    )


@pytest.fixture
def fixed_now(monkeypatch: pytest.MonkeyPatch) -> datetime:
    import forge_execute.runner as runner_module

    monkeypatch.setattr(runner_module, "datetime", _FixedDatetime)
    return _FIXED_NOW


def test_day_cap_blocks_run_before_any_work(
    tmp_path: Path, fixed_now: datetime
) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    # Heute bereits 2.00 USD verbraucht == Tages-Cap
    _seed_finished_run(
        store, run_id="r-prev", cost="2.00", ts=fixed_now.replace(hour=8)
    )
    runner = _make_runner(tmp_path, store)

    result = runner.run()

    assert result.decision == "cost_cap_hit"
    assert result.generations == []

    events = store.events_for_run(runner.run_id)
    kinds = [e.kind for e in events]
    assert kinds == [
        EventKind.RUN_STARTED,
        EventKind.COST_CAP_HIT,
        EventKind.RUN_FINISHED,
    ]
    cap_evt = events[1]
    assert cap_evt.payload["level"] == "project_day"
    assert Decimal(cap_evt.payload["actual_usd"]) == Decimal("2.00")
    # Kein Worktree wurde angelegt
    worktree_root = runner.worktrees.worktree_root
    assert not any(worktree_root.iterdir())


def test_month_cap_blocks_run(tmp_path: Path, fixed_now: datetime) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    # Heute nichts verbraucht, aber frueher im Monat das Monatsbudget
    _seed_finished_run(
        store,
        run_id="r-prev",
        cost="10.00",
        ts=fixed_now.replace(day=5),
    )
    runner = _make_runner(tmp_path, store)

    result = runner.run()

    assert result.decision == "cost_cap_hit"
    events = store.events_for_run(runner.run_id)
    cap_evt = next(e for e in events if e.kind == EventKind.COST_CAP_HIT)
    assert cap_evt.payload["level"] == "project_month"


def test_under_budget_does_not_block(tmp_path: Path, fixed_now: datetime) -> None:
    store = EventStore(tmp_path / "events.duckdb")
    _seed_finished_run(
        store, run_id="r-prev", cost="1.99", ts=fixed_now.replace(hour=8)
    )
    runner = _make_runner(tmp_path, store)

    assert runner._check_project_cost_caps() is None
    assert store.events_for_run(runner.run_id) == []
