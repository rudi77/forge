"""Arbeit erzeugen — die Effekt-Schicht zu ``intake.py`` (Roadmap A, Loop 2).

* :func:`refined_spec_text` / :func:`publish_spec` — A1: die im requirements-
  Run verdichteten Akzeptanzkriterien werden zum Artefakt: als Spec-PR
  (``docs/specs/<n>-<slug>.md``, Label ``forge:spec``) oder als Kommentar am
  Work-Item. Alle späteren Runs bekommen sie als Akzeptanzkriterium.
* :func:`intake_blocks` — A2: ``FORGE-WORKITEMS``-Blöcke aus Runs/Reviews →
  Items über den ``WorkTracker`` + ``WorkItemCreated``-Events.
* :func:`tick_sources` — A2: deterministische Quellen pro Conductor-Tick
  (CI rot auf ``main``, fällige ``schedule``-Trigger).

Alles hier ist Fabrik-Ebene: der Runner liefert nur Rohdaten, entscheidet und
legt nichts an (Mantra 3).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from forge_adapters.base import CodeHostError, TrackerError
from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    WorkItemCreatedPayload,
    build_event,
)
from forge_core.tracking import ReadyIssue
from forge_execute.capabilities import Capabilities
from forge_execute.worktrees import GitError, WorktreeManager
from ulid import ULID

from forge_cli.conductor import SPEC_PR_LABEL
from forge_cli.intake import (
    IntakeResult,
    ProposedItem,
    create_items,
    created_in_last_day,
    parse_workitems,
)
from forge_cli.runtime import ForgeContext, console, err_console
from forge_cli.schedule import cron_due

# --- A1: Specs ------------------------------------------------------------------


def refined_spec_text(store: Any, blobs: Any, issue_number: int) -> str:
    """Jüngste verdichtete Spec (``RequirementsRefined.artifacts['spec']``)."""
    run_ids = {
        e.run_id
        for e in store.events_by_kind(EventKind.RUN_STARTED)
        if (e.payload or {}).get("issue_number") == issue_number
    }
    refined = [
        e
        for e in store.events_by_kind(EventKind.REQUIREMENTS_REFINED)
        if e.run_id in run_ids and not (e.payload or {}).get("insufficient_context")
    ]
    if not refined:
        return ""
    blob = (max(refined, key=lambda e: e.ts).artifacts or {}).get("spec")
    if not blob:
        return ""
    try:
        return blobs.get_text(blob)
    except (FileNotFoundError, OSError, ValueError):
        return ""


def with_refined_spec(text: str, spec_md: str) -> str:
    """Hängt die verdichteten Akzeptanzkriterien an Prompt/Akzeptanztext."""
    if not spec_md.strip():
        return text
    return f"{text}\n\n## Refined acceptance criteria (forge requirements stage)\n\n{spec_md}"


def item_kind(issue: ReadyIssue) -> str:
    """Work-Item-Typ: Tracker-Typ (Azure) > ``type:<kind>``-Label > ``bug``."""
    if issue.kind:
        return issue.kind
    for lbl in issue.labels:
        if lbl.startswith("type:"):
            return lbl.removeprefix("type:")
    return "bug" if "bug" in issue.labels else "feature"


def _slug(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50] or "spec"


def publish_spec(
    ctx: ForgeContext,
    *,
    issue: ReadyIssue,
    run_id: str,
    store: Any,
    base_ref: str = "HEAD",
    pr_base: str = "main",
) -> str | None:
    """Veröffentlicht die Spec eines Items (A1). Liefert ``"pr"``/``"comment"``
    oder ``None`` (nichts veröffentlicht). Best-effort: Fehler werden gemeldet,
    blockieren aber den Conductor nicht."""
    cfg = ctx.spec.intake
    spec_md = refined_spec_text(store, ctx.open_blobs(), issue.number)
    if not spec_md or cfg.spec_publish == "off":
        return None
    mode = cfg.spec_publish
    if mode == "auto":
        mode = "comment" if item_kind(issue) in ("bug", "task") else "pr"
    caps = Capabilities(ctx.spec)
    if mode == "comment":
        if not caps.check_action("comment_issue").allowed:
            return None
        try:
            ctx.get_tracker().comment(
                number=issue.number,
                body=f"## forge: Akzeptanzkriterien\n\n{spec_md}",
            )
        except TrackerError as exc:
            err_console.print(f"[yellow]spec comment failed[/yellow] #{issue.number}: {exc}")
            return None
        return "comment"
    if not caps.check_action("open_pr").allowed:
        return None
    return "pr" if _open_spec_pr(
        ctx, issue=issue, run_id=run_id, store=store, spec_md=spec_md,
        base_ref=base_ref, pr_base=pr_base,
    ) else None


def _open_spec_pr(
    ctx: ForgeContext,
    *,
    issue: ReadyIssue,
    run_id: str,
    store: Any,
    spec_md: str,
    base_ref: str,
    pr_base: str,
) -> bool:
    rel = f"{ctx.spec.intake.spec_dir.rstrip('/')}/{issue.number}-{_slug(issue.title)}.md"
    wm = WorktreeManager(ctx.repo_root)
    try:
        wt = wm.create(run_id=f"spec-{issue.number}-{ULID()}", base_ref=base_ref)
    except GitError as exc:
        err_console.print(f"[yellow]spec worktree failed[/yellow] #{issue.number}: {exc}")
        return False
    try:
        path = wt.path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Spec: {issue.title}\n\n_Work-Item #{issue.number}_\n\n{spec_md.strip()}\n",
            encoding="utf-8",
        )
        wm.commit(wt, f"forge: spec for #{issue.number}", paths=[rel])
        labels = [SPEC_PR_LABEL, "forge:auto"]
        pr = ctx.get_code_host().open_change(
            branch=wt.branch,
            title=f"spec: #{issue.number} {issue.title}",
            body=(
                f"Spec für #{issue.number}, verdichtet von forge (requirements-Stage).\n\n"
                "Ein Merge gibt die Akzeptanzkriterien frei → das Item wandert nach "
                "`forge:design`. Änderungen am Text vor dem Merge sind ausdrücklich "
                "erwünscht.\n"
            ),
            base=pr_base,
            labels=labels,
        )
    except (GitError, CodeHostError, OSError) as exc:
        err_console.print(f"[yellow]spec PR failed[/yellow] #{issue.number}: {exc}")
        return False
    finally:
        wm.cleanup(wt)
    store.append(
        build_event(
            kind=EventKind.PR_CREATED,
            run_id=run_id,
            project=ctx.spec.name,
            project_fingerprint=ctx.project_fingerprint,
            factory_version=ctx.factory_version,
            spec_version=ctx.spec.spec_version,
            payload=PRCreatedPayload(
                pr_number=pr.pr_number, branch=pr.branch, base_branch=pr_base,
                labels=labels, url=pr.url,
            ),
        )
    )
    console.print(f"  [green]spec PR[/green] #{pr.pr_number} für #{issue.number} ({rel})")
    return True


# --- A2: Items anlegen --------------------------------------------------------------


def create_proposed(
    ctx: ForgeContext,
    *,
    store: Any,
    run_id: str,
    items: list[ProposedItem],
    source: str,
    parent: int | None = None,
    parent_approved: bool = False,
    origin_issue: int | None = None,
    scope: str = "",
    extra_lines: tuple[str, ...] = (),
) -> IntakeResult:
    """Legt ``items`` mit allen Leitplanken an und emittiert ``WorkItemCreated``."""
    if not items:
        return IntakeResult()
    now = datetime.now(UTC)
    today = created_in_last_day(store.events_by_kind(EventKind.WORK_ITEM_CREATED), now)

    def emit(payload: dict) -> None:
        store.append(
            build_event(
                kind=EventKind.WORK_ITEM_CREATED,
                run_id=run_id,
                project=ctx.spec.name,
                project_fingerprint=ctx.project_fingerprint,
                factory_version=ctx.factory_version,
                spec_version=ctx.spec.spec_version,
                payload=WorkItemCreatedPayload(**payload),
            )
        )

    try:
        tracker = ctx.get_tracker()
    except TrackerError as exc:
        return IntakeResult(skipped=[str(exc)])
    result = create_items(
        items,
        tracker=tracker,
        emit=emit,
        cfg=ctx.spec.intake,
        allowed=Capabilities(ctx.spec).check_action("create_work_items").allowed,
        source=source,
        created_today=today,
        parent=parent,
        parent_approved=parent_approved,
        origin_issue=origin_issue,
        scope=scope,
        provider=getattr(tracker, "provider", None),
        extra_lines=extra_lines,
    )
    if result.created:
        console.print(
            f"  [green]intake[/green] ({source}): created "
            + ", ".join(f"#{n}" for n in result.created)
        )
    for reason in result.skipped:
        err_console.print(f"  [dim]intake skipped: {reason}[/dim]")
    return result


def intake_blocks(
    ctx: ForgeContext,
    *,
    store: Any,
    run_id: str,
    blocks: list[str],
    source: str,
    origin_issue: int | None = None,
    parent: int | None = None,
    parent_approved: bool = False,
    extra_lines: tuple[str, ...] = (),
) -> IntakeResult:
    """Parst ``FORGE-WORKITEMS``-Blöcke eines Runs/Reviews und legt die Items an."""
    items: list[ProposedItem] = []
    for block in blocks:
        items.extend(parse_workitems(block))
    return create_proposed(
        ctx, store=store, run_id=run_id, items=items, source=source,
        origin_issue=origin_issue, parent=parent, parent_approved=parent_approved,
        extra_lines=extra_lines,
    )


# --- A2: Quellen pro Tick ------------------------------------------------------------


def tick_sources(ctx: ForgeContext, *, store: Any, session_id: str, now: datetime) -> int:
    """CI rot auf dem beobachteten Branch + fällige Schedules → Items.

    Gibt die Zahl angelegter Items zurück. Ohne ``create_work_items`` ein
    No-op (spart auch die Code-Host-Abfrage)."""
    if not Capabilities(ctx.spec).check_action("create_work_items").allowed:
        return 0
    created = 0
    branch = ctx.spec.intake.watch_main_ci
    if branch:
        try:
            status = ctx.get_code_host().ref_ci_status(branch)
        except CodeHostError:
            status = "unknown"
        if status == "fail":
            item = ProposedItem(
                local_id="ci",
                kind="bug",
                title=f"CI is red on {branch}",
                body=(
                    f"The CI of `{branch}` is failing. Find the commit that broke it "
                    "and restore a green build. Do not skip or weaken tests."
                ),
            )
            created += len(create_proposed(
                ctx, store=store, run_id=session_id, items=[item], source="ci_main_red",
                scope=f"{branch}|{now.date().isoformat()}",
            ).created)

    prior = store.events_by_kind(EventKind.WORK_ITEM_CREATED)
    for trig in ctx.spec.triggers.schedule:
        title = f"[schedule] {trig.focus}"
        last = max(
            (e.ts for e in prior
             if (e.payload or {}).get("source") == "schedule"
             and (e.payload or {}).get("title") == title),
            default=None,
        )
        if not cron_due(trig.cron, last=last, now=now):
            continue
        item = ProposedItem(
            local_id="s",
            kind="epic",
            title=title,
            body=(
                f"Scheduled focus `{trig.focus}` (cron `{trig.cron}`). Decompose into "
                "small, independently shippable work items with testable acceptance "
                "criteria."
            ),
        )
        created += len(create_proposed(
            ctx, store=store, run_id=session_id, items=[item], source="schedule",
            scope=f"{trig.cron}|{now.strftime('%Y-%m-%dT%H:%M')}",
        ).created)
    return created


__all__ = [
    "create_proposed",
    "intake_blocks",
    "item_kind",
    "publish_spec",
    "refined_spec_text",
    "tick_sources",
    "with_refined_spec",
]
