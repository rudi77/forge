"""Tests für die Conductor-Board-Operationen (list_stage_items, set_label).

Pattern wie ``test_board.py``: gh-CLI via injizierter ``run_subprocess``-Stub,
kein echter gh-Aufruf. Verifiziert Kommando-Konstruktion + JSON-Parsing +
client-seitige Stage-Filterung — denselben Verifikations-Standard wie der
bestehende Board-Adapter.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from forge_adapters.github.board import (
    BoardError,
    list_stage_items,
    set_issue_stage_label,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=0, stdout=stdout, stderr=stderr
    )


def _fail(stderr: str, code: int = 1) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=[], returncode=code, stdout="", stderr=stderr
    )


def _stub_runner(*responses: subprocess.CompletedProcess):
    iterator = iter(responses)
    calls: list[list[str]] = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return next(iterator)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# --- list_stage_items -----------------------------------------------------


def test_list_stage_items_filters_to_stage_labels() -> None:
    payload = json.dumps(
        [
            {
                "number": 5,
                "title": "feature A",
                "body": "Depends-On: #3",
                "labels": [{"name": "forge:ready"}, {"name": "bug"}],
                "url": "https://github.com/x/y/issues/5",
            },
            {
                "number": 6,
                "title": "no stage label",
                "body": "",
                "labels": [{"name": "bug"}],
                "url": "https://github.com/x/y/issues/6",
            },
            {
                "number": 4,
                "title": "in qa",
                "body": "",
                "labels": [{"name": "forge:qa"}],
                "url": "https://github.com/x/y/issues/4",
            },
        ]
    )
    runner = _stub_runner(_ok(stdout=payload))
    items = list_stage_items(
        repo_owner="x",
        repo_name="y",
        stage_labels=["forge:ready", "forge:qa", "forge:design"],
        run_subprocess=runner,
    )
    # #6 (kein Stage-Label) raus; Rest nach Nummer sortiert.
    assert [i.number for i in items] == [4, 5]
    assert items[1].body == "Depends-On: #3"
    # Kommando-Form
    cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert cmd[:3] == ["gh", "issue", "list"]
    assert "--json" in cmd and "number,title,body,labels,url" in cmd


def test_list_stage_items_empty() -> None:
    runner = _stub_runner(_ok(stdout="[]"))
    assert (
        list_stage_items(
            repo_owner="x", repo_name="y", stage_labels=["forge:ready"],
            run_subprocess=runner,
        )
        == []
    )


def test_list_stage_items_gh_error() -> None:
    runner = _stub_runner(_fail("gh boom"))
    with pytest.raises(BoardError, match="gh issue list failed"):
        list_stage_items(
            repo_owner="x", repo_name="y", stage_labels=["forge:ready"],
            run_subprocess=runner,
        )


def test_list_stage_items_bad_json() -> None:
    runner = _stub_runner(_ok(stdout="not json"))
    with pytest.raises(BoardError, match="invalid JSON"):
        list_stage_items(
            repo_owner="x", repo_name="y", stage_labels=["forge:ready"],
            run_subprocess=runner,
        )


# --- set_issue_stage_label ------------------------------------------------


def test_set_issue_stage_label_add_and_remove() -> None:
    runner = _stub_runner(_ok())
    set_issue_stage_label(
        issue_number=5,
        repo_owner="x",
        repo_name="y",
        add="forge:in-dev",
        remove="forge:ready",
        run_subprocess=runner,
    )
    cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert cmd[:4] == ["gh", "issue", "edit", "5"]
    assert "--add-label" in cmd and "forge:in-dev" in cmd
    assert "--remove-label" in cmd and "forge:ready" in cmd


def test_set_issue_stage_label_add_only() -> None:
    runner = _stub_runner(_ok())
    set_issue_stage_label(
        issue_number=7,
        repo_owner="x",
        repo_name="y",
        add="forge:blocked",
        run_subprocess=runner,
    )
    cmd = runner.calls[0]  # type: ignore[attr-defined]
    assert "--add-label" in cmd and "forge:blocked" in cmd
    assert "--remove-label" not in cmd


def test_set_issue_stage_label_gh_error() -> None:
    runner = _stub_runner(_fail("permission denied"))
    with pytest.raises(BoardError, match="gh issue edit #9 failed"):
        set_issue_stage_label(
            issue_number=9,
            repo_owner="x",
            repo_name="y",
            add="forge:qa",
            run_subprocess=runner,
        )
