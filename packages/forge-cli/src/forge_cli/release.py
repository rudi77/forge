"""Release-Train (Roadmap L3, Loop 2).

Statt eines ``forge-issue-<N>``-Tags pro Item sammeln sich fertige Items in
``forge:release``. Ein Train-Tick:

1. **Vorbereiten** — warten ≥ ``min_items`` (und ist ``schedule`` fällig),
   berechnet forge aus den Commits seit dem letzten Tag die nächste SemVer
   (:func:`next_version`), schreibt den Changelog-Abschnitt
   (:func:`render_changelog`) + hebt ``version_files`` an und öffnet einen
   **Release-PR** ``chore(release): vX.Y.Z`` (Label ``forge:release-pr``).
   ``push_to_main`` bleibt verboten — die Änderung kommt nur per PR.
2. **Absichern** — der Release-PR geht durch denselben Agent-Review +
   ``merge_pr``-Mehrfachbedingung wie jeder andere PR (oder ein Mensch merged).
3. **Taggen** — nach dem Merge: Tag ``<prefix>X.Y.Z`` auf dem Basis-Branch,
   Release mit dem Changelog-Abschnitt als Notes, ``ReleaseTagged`` mit allen
   ausgelieferten ``issue_numbers`` → ``release_done`` → ``done``.

Versionierung und Changelog sind **rein** (deterministisch, voll unit-
getestet); die Effekte (git, Code-Host, Events) stehen in :func:`train_tick`.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from forge_adapters.base import CodeHostError
from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    ReleaseTaggedPayload,
    build_event,
)
from forge_execute.capabilities import Capabilities
from forge_execute.worktrees import GitError, WorktreeManager
from ulid import ULID

from forge_cli.runtime import ForgeContext, console
from forge_cli.schedule import cron_due

RELEASE_PR_LABEL = "forge:release-pr"
_ITEMS_LINE = re.compile(r"(?im)^release-items\s*:\s*(.*)$")

# --- SemVer -----------------------------------------------------------------------


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @classmethod
    def parse(cls, text: str, prefix: str = "") -> SemVer | None:
        m = re.fullmatch(re.escape(prefix) + r"(\d+)\.(\d+)\.(\d+)", text.strip())
        return cls(*(int(g) for g in m.groups())) if m else None


ZERO = SemVer(0, 0, 0)


def latest_version(tags: list[str], prefix: str = "v") -> SemVer | None:
    versions = [v for t in tags if (v := SemVer.parse(t, prefix)) is not None]
    return max(versions) if versions else None


@dataclass(frozen=True)
class Commit:
    subject: str
    body: str = ""


_CC = re.compile(r"^(?P<type>[a-zA-Z]+)(?:\((?P<scope>[^)]*)\))?(?P<bang>!)?:\s*(?P<desc>.+)$")


def bump_kind(commit: Commit) -> str | None:
    """``major`` | ``minor`` | ``patch`` | ``None`` für einen Conventional Commit."""
    if "BREAKING CHANGE" in commit.body or "BREAKING-CHANGE" in commit.body:
        return "major"
    m = _CC.match(commit.subject.strip())
    if not m:
        return None
    if m.group("bang"):
        return "major"
    kind = m.group("type").lower()
    if kind == "feat":
        return "minor"
    if kind in ("fix", "perf"):
        return "patch"
    return None


def next_version(
    last: SemVer | None, commits: list[Commit], *, conventional: bool = True
) -> SemVer:
    """Nächste Version. Vor 1.0 hebt ein Breaking Change nur die Minor-Stelle
    (SemVer-Konvention für 0.x). Ohne relevante Commits → Patch."""
    base = last or ZERO
    kinds = {bump_kind(c) for c in commits} if conventional else set()
    if "major" in kinds:
        if base.major == 0:
            return SemVer(0, base.minor + 1, 0)
        return SemVer(base.major + 1, 0, 0)
    if "minor" in kinds:
        return SemVer(base.major, base.minor + 1, 0)
    if last is None:
        return SemVer(0, 1, 0)
    return SemVer(base.major, base.minor, base.patch + 1)


_SECTIONS = (
    ("major", "Breaking changes"),
    ("minor", "Features"),
    ("patch", "Fixes"),
    (None, "Other changes"),
)
_SKIP = re.compile(r"^(chore\(release\)|forge: spec for|Merge )", re.IGNORECASE)


def render_changelog(
    version: SemVer, commits: list[Commit], *, day: date, issues: list[int] | None = None
) -> str:
    """Changelog-Abschnitt (Keep-a-Changelog-Stil), gruppiert nach Bump-Art."""
    lines = [f"## [{version}] — {day.isoformat()}", ""]
    groups: dict[str | None, list[str]] = {}
    for c in commits:
        if _SKIP.match(c.subject):
            continue
        m = _CC.match(c.subject.strip())
        text = m.group("desc") if m else c.subject.strip()
        if m and m.group("scope"):
            text = f"**{m.group('scope')}:** {text}"
        groups.setdefault(bump_kind(c), []).append(text)
    for key, title in _SECTIONS:
        if groups.get(key):
            lines += [f"### {title}", "", *(f"- {t}" for t in groups[key]), ""]
    if issues:
        lines += ["Work items: " + ", ".join(f"#{n}" for n in sorted(issues)), ""]
    return "\n".join(lines).rstrip() + "\n"


def prepend_changelog(existing: str, section: str) -> str:
    """Neuen Abschnitt vor den ersten ``## ``-Eintrag setzen (Titel bleibt oben)."""
    if not existing.strip():
        return "# Changelog\n\n" + section
    idx = existing.find("\n## ")
    if idx == -1:
        return existing.rstrip() + "\n\n" + section
    return existing[: idx + 1] + section + "\n" + existing[idx + 1 :]


def bump_version_file(name: str, text: str, version: SemVer) -> str:
    """Hebt die Version in ``pyproject.toml``/``package.json``/``VERSION`` an.
    Unbekannte Formate bleiben unverändert (lieber keine als eine falsche)."""
    v = str(version)
    base = Path(name).name
    if base == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return text
        data["version"] = v
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    if base.endswith(".toml"):
        return re.sub(r'(?m)^(version\s*=\s*)"[^"]*"', rf'\g<1>"{v}"', text, count=1)
    if base.upper() in ("VERSION", "VERSION.TXT"):
        return v + "\n"
    return text


def parse_release_items(body: str) -> list[int]:
    m = _ITEMS_LINE.search(body or "")
    return [int(n) for n in re.findall(r"#(\d+)", m.group(1))] if m else []


# --- Effekte -------------------------------------------------------------------------


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


def collect_commits(repo: Path, *, base_ref: str, since_tag: str | None) -> list[Commit]:
    rng = f"{since_tag}..{base_ref}" if since_tag else base_ref
    out = _git(["log", "--format=%s%x1f%b%x1e", rng], repo)
    if out.returncode != 0:
        return []
    commits = []
    for rec in out.stdout.split("\x1e"):
        rec = rec.strip("\n")
        if not rec.strip():
            continue
        subject, _, body = rec.partition("\x1f")
        commits.append(Commit(subject.strip(), body.strip()))
    return commits


def _release_prs(events: list) -> list:
    return [
        e for e in events
        if e.kind == EventKind.PR_CREATED
        and RELEASE_PR_LABEL in ((e.payload or {}).get("labels") or [])
    ]


@dataclass(frozen=True)
class TrainResult:
    action: str
    """``idle`` | ``prepared`` | ``waiting`` | ``reviewed`` | ``tagged`` |
    ``abandoned`` | ``error``."""
    detail: str = ""


def train_tick(
    ctx: ForgeContext,
    *,
    store: Any,
    session_id: str,
    events: list,
    waiting_items: list[int],
    base: str,
    now: datetime,
    review_fn=None,
) -> TrainResult:
    """Ein Schritt des Release-Trains (idempotent über den Event-Strom)."""
    cfg = ctx.spec.release
    caps = Capabilities(ctx.spec)
    if not caps.check_action("create_release").allowed:
        return TrainResult("idle", "capabilities.create_release=false")
    host = ctx.get_code_host()
    merged = {(e.payload or {}).get("pr_number") for e in events if e.kind == EventKind.PR_MERGED}
    tagged = {(e.payload or {}).get("tag") for e in events if e.kind == EventKind.RELEASE_TAGGED}
    pending = [
        e for e in _release_prs(events)
        if f"{cfg.tag_prefix}{_version_from_branch((e.payload or {}).get('branch'))}"
        not in tagged
    ]
    if pending:
        # Nur der jüngste Release-PR zählt; wurde er ungemergt geschlossen,
        # bereitet der Train einen neuen vor.
        pr_evt = max(pending, key=lambda e: e.ts)
        res = _advance_release_pr(
            ctx, store=store, session_id=session_id, events=events, pr_evt=pr_evt,
            merged=merged, review_fn=review_fn,
        )
        if res.action != "abandoned":
            return res
    if len(waiting_items) < cfg.min_items:
        return TrainResult("idle", f"{len(waiting_items)} < min_items")
    if cfg.schedule:
        # Erstes Mal ohne Historie: das letzte Tagesfenster zählt (sonst feuert
        # cron_due nur, wenn ein Tick exakt die Minute trifft).
        last = max((e.ts for e in _release_prs(events)), default=None) or (
            now - timedelta(days=1)
        )
        if not cron_due(cfg.schedule, last=last, now=now):
            return TrainResult("idle", "schedule not due")
    if not caps.check_action("open_pr").allowed:
        return TrainResult("idle", "capabilities.open_pr=false")
    return _prepare_release_pr(
        ctx, store=store, session_id=session_id, items=waiting_items, base=base, now=now,
        host=host,
    )


_BRANCH_VERSION = re.compile(r"release-(\d+\.\d+\.\d+)")


def _version_from_branch(branch: str | None) -> str:
    m = _BRANCH_VERSION.search(branch or "")
    return m.group(1) if m else ""


def _prepare_release_pr(ctx, *, store, session_id, items, base, now, host) -> TrainResult:
    cfg = ctx.spec.release
    wm = WorktreeManager(ctx.repo_root)
    try:
        base_ref = wm.fetch_remote_branch(base)
    except GitError as exc:
        return TrainResult("error", str(exc))
    _git(["fetch", "--tags", "--quiet", "origin"], ctx.repo_root)
    tags = _git(["tag", "-l", f"{cfg.tag_prefix}*"], ctx.repo_root).stdout.split()
    last = latest_version(tags, cfg.tag_prefix)
    last_tag = f"{cfg.tag_prefix}{last}" if last else None
    commits = collect_commits(ctx.repo_root, base_ref=base_ref, since_tag=last_tag)
    version = next_version(last, commits, conventional=cfg.conventional_commits)
    section = render_changelog(version, commits, day=now.date(), issues=items)
    run_id = f"release-{version}-{str(ULID())[-8:].lower()}"
    try:
        wt = wm.create(run_id=run_id, base_ref=base_ref)
    except GitError as exc:
        return TrainResult("error", str(exc))
    target_branch = f"forge/{run_id}"
    try:
        changelog = cfg.changelog or "CHANGELOG.md"
        path = wt.path / changelog
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(prepend_changelog(existing, section), encoding="utf-8")
        touched = [changelog]
        for vf in cfg.version_files:
            vpath = wt.path / vf
            if vpath.exists():
                old = vpath.read_text(encoding="utf-8")
                new = bump_version_file(vf, old, version)
                if new != old:
                    vpath.write_text(new, encoding="utf-8")
                    touched.append(vf)
        wm.commit(wt, f"chore(release): {cfg.tag_prefix}{version}", paths=touched)
        host.push_branch(branch=wt.branch, target=target_branch)
        labels = [RELEASE_PR_LABEL, "forge:auto"]
        pr = host.open_change(
            branch=target_branch,
            title=f"chore(release): {cfg.tag_prefix}{version}",
            # Release-Items ZUERST: Azure kürzt Beschreibungen auf 4000 Zeichen,
            # die Zeile darf bei langen Changelogs nicht verloren gehen.
            body=(
                "Release-Items: " + ", ".join(f"#{n}" for n in sorted(items)) + "\n\n"
                f"Release {cfg.tag_prefix}{version}, vorbereitet von forge (Release-Train).\n\n"
                + section
            ),
            base=base,
            labels=labels,
            push=False,
        )
    except (GitError, CodeHostError, OSError) as exc:
        return TrainResult("error", str(exc))
    finally:
        wm.cleanup(wt)
        wm.cleanup_branch(wt)
    store.append(build_event(
        kind=EventKind.PR_CREATED, run_id=session_id, project=ctx.spec.name,
        project_fingerprint=ctx.project_fingerprint, factory_version=ctx.factory_version,
        spec_version=ctx.spec.spec_version,
        payload=PRCreatedPayload(pr_number=pr.pr_number, branch=target_branch, base_branch=base,
                                 labels=labels, url=pr.url),
    ))
    console.print(
        f"  [green]release PR[/green] #{pr.pr_number} {cfg.tag_prefix}{version} "
        f"({len(items)} items)"
    )
    return TrainResult("prepared", f"{cfg.tag_prefix}{version}")


def _advance_release_pr(ctx, *, store, session_id, events, pr_evt, merged, review_fn):
    cfg = ctx.spec.release
    host = ctx.get_code_host()
    pr_number = (pr_evt.payload or {}).get("pr_number")
    branch = (pr_evt.payload or {}).get("branch")
    base = (pr_evt.payload or {}).get("base_branch") or "main"
    try:
        meta = host.fetch_metadata(pr_number)
    except CodeHostError as exc:
        return TrainResult("error", str(exc))
    if pr_number not in merged and meta.state != "MERGED":
        if meta.state == "CLOSED":
            return TrainResult("abandoned", f"release PR #{pr_number} closed unmerged")
        if meta.state != "OPEN":
            return TrainResult("waiting", f"release PR #{pr_number} is {meta.state}")
        reviewed = any(
            e.kind == EventKind.PR_REVIEWED and (e.payload or {}).get("pr_number") == pr_number
            for e in events
        )
        if review_fn is not None and not reviewed and meta.ci_status in ("pass", "none"):
            review_fn(pr_number)
            return TrainResult("reviewed", f"release PR #{pr_number}")
        return TrainResult("waiting", f"release PR #{pr_number} awaits merge")
    version = SemVer.parse(_version_from_branch(branch))
    if version is None:
        return TrainResult("error", f"cannot read version from {branch!r}")
    tag = f"{cfg.tag_prefix}{version}"
    items = parse_release_items(meta.body)
    notes = _ITEMS_LINE.sub("", meta.body).strip() + "\n"
    try:
        url = host.create_release(tag=tag, title=tag, notes=notes, target=base)
    except CodeHostError as exc:
        return TrainResult("error", str(exc))
    blob = ctx.open_blobs().put_text(notes)
    store.append(build_event(
        kind=EventKind.RELEASE_TAGGED, run_id=session_id, project=ctx.spec.name,
        project_fingerprint=ctx.project_fingerprint, factory_version=ctx.factory_version,
        spec_version=ctx.spec.spec_version,
        payload=ReleaseTaggedPayload(
            issue_number=items[0] if items else pr_number,
            tag=tag, release_url=url or None, changelog_blob=blob, version=str(version),
            issue_numbers=items,
        ),
    ))
    console.print(f"  [green]released[/green] {tag} → {', '.join(f'#{n}' for n in items)}")
    return TrainResult("tagged", tag)


__all__ = [
    "RELEASE_PR_LABEL",
    "Commit",
    "SemVer",
    "TrainResult",
    "bump_kind",
    "bump_version_file",
    "collect_commits",
    "latest_version",
    "next_version",
    "parse_release_items",
    "prepend_changelog",
    "render_changelog",
    "train_tick",
]

