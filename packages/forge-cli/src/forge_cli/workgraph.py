"""Arbeitsgraph über Worktrees — die Effekt-Schicht zu Abschnitt G (Loop 2).

Die reine Planung (Konfliktkanten aus ``Touches:``, Kapazität) lebt in
``conductor.py``. Hier stehen die Effekte, die der Conductor-Tick braucht:

* :func:`tick_capacity` — Kapazität aus ``--max-parallel``, Tagesbudget,
  ⌀Run-Kosten (Event-Strom) und freiem Plattenplatz.
* :func:`sync_branch` — PR-Branch nach einem Geschwister-Merge nachziehen:
  deterministischer ``git merge`` der Basis in einem eigenen Worktree; sauber →
  Fast-Forward-Push; Konflikt → Konflikt-Commit auf einem lokalen Branch, von
  dem aus ein Agent-Run die Marker auflöst (rot→grün-Pfad der Loop, keine
  neue Loop-Logik).
* :func:`ensure_integration_branch` / :func:`open_integration_pr` — optionaler
  Integrations-Branch ``forge/epic-<N>`` pro Epic (E13).

Push-Leitplanken bleiben die aus ``git_push_argv``: nur ``forge/*``, nie
``--force``.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from forge_adapters.base import CodeHostError
from forge_core.events import EventKind, PRCreatedPayload, build_event
from forge_core.tracking import ReadyIssue
from forge_execute.worktrees import GitError, WorktreeManager
from ulid import ULID

from forge_cli.conductor import effective_capacity
from forge_cli.runtime import ForgeContext, console, err_console

# --- Kapazität -------------------------------------------------------------------


def tick_capacity(ctx: ForgeContext, store: Any, max_parallel: int) -> int:
    """``effective_capacity`` mit Werten aus Event-Strom + Dateisystem."""
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        spent = store.query(
            "SELECT COALESCE(SUM(cost_usd), 0) AS s FROM events WHERE ts >= ?", [today]
        )[0]["s"]
        avg = store.query(
            "SELECT AVG(total_cost_usd) AS a FROM (SELECT total_cost_usd FROM "
            "runs_with_outcomes WHERE total_cost_usd IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 20)"
        )[0]["a"]
    except Exception:  # Kapazität ist eine Optimierung — nie den Tick killen
        spent, avg = 0, None
    try:
        free = shutil.disk_usage(ctx.repo_root).free
    except OSError:
        free = None
    return effective_capacity(
        max_parallel,
        daily_cap_usd=float(ctx.spec.cost_caps.per_project_per_day_usd),
        spent_today_usd=float(spent or 0),
        avg_run_cost_usd=float(avg) if avg is not None else None,
        disk_free_bytes=free,
    )


# --- Geschwister nachziehen ------------------------------------------------------


@dataclass(frozen=True)
class SyncResult:
    status: str
    """``synced`` (sauber gemergt + gepusht), ``conflict`` (Agent nötig),
    ``skipped`` (kein forge-Branch) oder ``error``."""
    conflict_ref: str | None = None
    """Lokaler Branch mit dem Konflikt-Commit (Basis für den Agent-Run)."""
    files: tuple[str, ...] = ()
    head_branch: str = ""
    detail: str = ""


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def sync_branch(ctx: ForgeContext, pr_number: int) -> SyncResult:
    """Merged die PR-Basis in den PR-Branch (nie Rebase/Force)."""
    host = ctx.get_code_host()
    try:
        meta = host.fetch_metadata(pr_number)
    except CodeHostError as exc:
        return SyncResult("error", detail=str(exc))
    head, base = meta.head_branch, meta.base_branch
    if not head.startswith("forge/"):
        return SyncResult("skipped", head_branch=head, detail="not a forge/* branch")
    wm = WorktreeManager(ctx.repo_root)
    try:
        head_ref = wm.fetch_remote_branch(head)
        base_ref = wm.fetch_remote_branch(base)
        wt = wm.create(run_id=f"sync-{pr_number}-{ULID()}", base_ref=head_ref)
    except GitError as exc:
        return SyncResult("error", head_branch=head, detail=str(exc))
    drop_branch = True
    try:
        merge = _git(["-c", "user.name=forge", "-c", "user.email=forge@localhost",
                      "merge", "--no-edit", base_ref], wt.path)
        if merge.returncode == 0:
            try:
                host.push_branch(branch=wt.branch, target=head)
            except CodeHostError as exc:
                return SyncResult("error", head_branch=head, detail=str(exc))
            return SyncResult("synced", head_branch=head)
        files = tuple(
            f for f in _git(["diff", "--name-only", "--diff-filter=U"], wt.path)
            .stdout.splitlines() if f
        )
        # Konflikt-Marker bewusst committen: der Agent-Run startet von hier,
        # sieht die Marker, Tests/Gates sind rot → Auflösung = rot→grün-KEEP.
        _git(["add", "-A"], wt.path)
        commit = _git(["-c", "user.name=forge", "-c", "user.email=forge@localhost",
                       "commit", "--no-edit", "-m",
                       f"forge: merge {base} into {head} (conflicts to resolve)"], wt.path)
        if commit.returncode != 0:
            return SyncResult("error", head_branch=head, detail=commit.stderr.strip())
        drop_branch = False  # Basis des Konflikt-Runs
        return SyncResult("conflict", conflict_ref=wt.branch, files=files, head_branch=head)
    finally:
        wm.cleanup(wt)
        if drop_branch:
            wm.cleanup_branch(wt)


# --- Integrations-Branch pro Epic -------------------------------------------------


def integration_branch_name(epic_number: int) -> str:
    return f"forge/epic-{epic_number}"


def ensure_integration_branch(ctx: ForgeContext, epic_number: int, base: str) -> str | None:
    """Legt ``forge/epic-<N>`` auf dem Stand von ``base`` an (idempotent:
    existiert der Branch schon, bleibt er unverändert)."""
    branch = integration_branch_name(epic_number)
    wm = WorktreeManager(ctx.repo_root)
    try:
        wm.fetch_remote_branch(branch)
        return branch  # existiert bereits
    except GitError:
        pass
    try:
        base_ref = wm.fetch_remote_branch(base)
        ctx.get_code_host().push_branch(branch=base_ref, target=branch)
    except (GitError, CodeHostError) as exc:
        err_console.print(f"[yellow]integration branch failed[/yellow] #{epic_number}: {exc}")
        return None
    console.print(f"  [green]integration branch[/green] {branch} ← {base}")
    return branch


def open_integration_pr(
    ctx: ForgeContext,
    *,
    epic: ReadyIssue,
    run_id: str,
    store: Any,
    base: str,
) -> int | None:
    """Öffnet den PR ``forge/epic-<N> → base``, sobald alle Kinder integriert
    sind. Das ``PRCreated`` hängt am Epic-Run → ``has_open_pr`` für das Epic →
    ``tracking → qa`` (Review + Merge wie jeder andere PR)."""
    branch = integration_branch_name(epic.number)
    try:
        pr = ctx.get_code_host().open_change(
            branch=branch,
            title=f"forge: epic #{epic.number} {epic.title}",
            body=(
                f"Integration aller Kind-Items von Epic #{epic.number}.\n\n"
                "Jedes Kind wurde einzeln reviewt und in diesen Branch gemergt; "
                "dieser PR liefert das Epic als Ganzes aus.\n"
            ),
            base=base,
            labels=["forge:auto", "forge:epic"],
            push=False,
        )
    except CodeHostError as exc:
        err_console.print(f"[yellow]integration PR failed[/yellow] #{epic.number}: {exc}")
        return None
    store.append(
        build_event(
            kind=EventKind.PR_CREATED,
            run_id=run_id,
            project=ctx.spec.name,
            project_fingerprint=ctx.project_fingerprint,
            factory_version=ctx.factory_version,
            spec_version=ctx.spec.spec_version,
            payload=PRCreatedPayload(
                pr_number=pr.pr_number, branch=branch, base_branch=base,
                labels=["forge:auto", "forge:epic"], url=pr.url,
            ),
        )
    )
    console.print(f"  [green]integration PR[/green] #{pr.pr_number} für Epic #{epic.number}")
    return pr.pr_number


def epic_run_id(events: list, epic_number: int) -> str | None:
    """run_id des jüngsten Zerlegungs-Runs eines Epics (Anker für PRCreated)."""
    runs = [
        e for e in events
        if e.kind == EventKind.RUN_STARTED
        and (e.payload or {}).get("issue_number") == epic_number
        and str((e.payload or {}).get("focus") or "").startswith("epic:")
    ]
    return max(runs, key=lambda e: e.ts).run_id if runs else None


__all__ = [
    "SyncResult",
    "ensure_integration_branch",
    "epic_run_id",
    "integration_branch_name",
    "open_integration_pr",
    "sync_branch",
    "tick_capacity",
]
