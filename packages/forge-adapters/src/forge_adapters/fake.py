"""In-Memory-``WorkTracker``/``CodeHost`` für Tests und Dry-Runs.

Kein Subprozess, kein Netz: der Zustand lebt in Dicts und wird von den Tests
direkt gelesen/vorbelegt. Beide Klassen bestehen dieselbe Vertrags-Testsuite wie
die echten Adapter — damit ist garantiert, dass Conductor-Tests gegen die Fakes
dieselbe Semantik sehen wie gegen GitHub/Azure.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from forge_core.spec import BoardConfig
from forge_core.tracking import NewWorkItem, ReadyIssue

from forge_adapters.base import (
    CloseReason,
    CodeHostError,
    LabelReport,
    MergeMethod,
    MergeResult,
    OpenChange,
    PRCreationResult,
    PRMetadata,
    TrackerError,
)


@dataclass
class _FakeItem:
    issue: ReadyIssue
    open: bool = True
    comments: list[str] = field(default_factory=list)


class InMemoryTracker:
    """Work-Tracker im Speicher. ``items`` ist öffentlich für Asserts."""

    provider = "fake"

    def __init__(self, items: list[ReadyIssue] | None = None) -> None:
        self.items: dict[int, _FakeItem] = {}
        self.labels: set[str] = set()
        self.stage_calls: list[tuple[int, str, str | None]] = []
        for it in items or []:
            self.add(it)

    # --- Test-Helfer --------------------------------------------------------

    def add(self, issue: ReadyIssue, *, open_: bool = True) -> None:
        self.items[issue.number] = _FakeItem(issue=issue, open=open_)
        self.labels.update(issue.labels)

    def labels_of(self, number: int) -> list[str]:
        return list(self.items[number].issue.labels)

    def comments_of(self, number: int) -> list[str]:
        return list(self.items[number].comments)

    def is_open(self, number: int) -> bool:
        return self.items[number].open

    # --- WorkTracker ---------------------------------------------------------

    def list_ready_items(self, board: BoardConfig) -> list[ReadyIssue]:
        wanted = set(board.filter_labels)
        return sorted(
            (
                f.issue
                for f in self.items.values()
                if f.open and wanted.issubset(f.issue.labels)
            ),
            key=lambda i: i.number,
        )

    def list_stage_items(
        self, *, stage_labels: list[str], state: str = "open"
    ) -> list[ReadyIssue]:
        wanted = set(stage_labels)
        out = []
        for f in self.items.values():
            if state == "open" and not f.open:
                continue
            if state == "closed" and f.open:
                continue
            if wanted.intersection(f.issue.labels):
                out.append(f.issue)
        return sorted(out, key=lambda i: i.number)

    def get_items(self, numbers: list[int]) -> list[ReadyIssue]:
        missing = [n for n in numbers if n not in self.items]
        if missing:
            raise TrackerError(f"unknown items: {missing}")
        return [self.items[n].issue for n in numbers]

    def set_stage(self, *, number: int, add: str, remove: str | None = None) -> None:
        if number not in self.items:
            raise TrackerError(f"unknown item #{number}")
        self.stage_calls.append((number, add, remove))
        f = self.items[number]
        labels = [lbl for lbl in f.issue.labels if lbl != remove]
        if add not in labels:
            labels.append(add)
        f.issue = replace(f.issue, labels=labels)
        self.labels.add(add)

    def comment(self, *, number: int, body: str) -> None:
        if number not in self.items:
            raise TrackerError(f"unknown item #{number}")
        self.items[number].comments.append(body)

    def close(self, *, number: int, reason: CloseReason = "not planned") -> None:
        if number not in self.items:
            raise TrackerError(f"unknown item #{number}")
        self.items[number].open = False

    def ensure_labels(self, labels: list[str], *, create: bool = False) -> LabelReport:
        present = [lbl for lbl in labels if lbl in self.labels]
        missing = [lbl for lbl in labels if lbl not in self.labels]
        if create:
            self.labels.update(missing)
            return LabelReport(present=present, missing=[], created=missing)
        return LabelReport(present=present, missing=missing, created=[])

    def create_item(self, item: NewWorkItem) -> ReadyIssue:
        number = max(self.items, default=0) + 1
        labels = [*item.labels, f"type:{item.kind}"]
        body = item.body
        if item.parent is not None and f"#{item.parent}" not in body:
            body = f"{body}\n\nParent: #{item.parent}"
        issue = ReadyIssue(
            number=number,
            title=item.title,
            body=body,
            labels=labels,
            project_status="",
            url=f"fake://items/{number}",
            kind=item.kind,
            parent=item.parent,
        )
        self.add(issue)
        return issue

    def search_items(self, text: str) -> list[ReadyIssue]:
        return sorted(
            (f.issue for f in self.items.values() if text in f.issue.body),
            key=lambda i: i.number,
        )


@dataclass
class FakePR:
    number: int
    branch: str
    base: str
    title: str
    body: str
    labels: list[str]
    state: str = "OPEN"
    ci_status: str = "pass"
    mergeable: str = "MERGEABLE"
    head_committed_at: datetime | None = None
    reviews: list[tuple[bool, str]] = field(default_factory=list)
    diff: str = ""
    ci_failure: str = ""


class InMemoryCodeHost:
    """Code-Host im Speicher. ``prs``/``pushed``/``releases`` sind öffentlich."""

    provider = "fake"

    def __init__(self) -> None:
        self.prs: dict[int, FakePR] = {}
        self.pushed: list[str] = []
        self.releases: dict[str, tuple[str, str | None]] = {}
        self.auto_merge_queued: list[int] = []
        self.ref_status: dict[str, str] = {}
        self._next = 100

    def push_branch(self, *, branch: str, remote: str = "origin") -> None:
        self.pushed.append(branch)

    def open_change(
        self,
        *,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        labels: list[str] | None = None,
        draft: bool = False,
        push: bool = True,
    ) -> PRCreationResult:
        if push:
            self.push_branch(branch=branch)
        self._next += 1
        n = self._next
        self.prs[n] = FakePR(
            number=n, branch=branch, base=base, title=title, body=body,
            labels=list(labels or ["forge:auto"]),
        )
        return PRCreationResult(pr_number=n, url=f"fake://pr/{n}", branch=branch)

    def queue_auto_merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> None:
        self._pr(pr_number)
        self.auto_merge_queued.append(pr_number)

    def fetch_metadata(self, pr_number: int) -> PRMetadata:
        pr = self._pr(pr_number)
        return PRMetadata(
            number=pr.number, title=pr.title, body=pr.body, state=pr.state,
            base_branch=pr.base, head_branch=pr.branch, ci_status=pr.ci_status,
            mergeable=pr.mergeable,
        )

    def fetch_diff(self, pr_number: int) -> str:
        return self._pr(pr_number).diff

    def head_committed_at(self, pr_number: int) -> datetime | None:
        pr = self.prs.get(pr_number)
        return pr.head_committed_at if pr else None

    def post_review(self, *, pr_number: int, approve: bool, body: str) -> None:
        self._pr(pr_number).reviews.append((approve, body))

    def merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> MergeResult:
        pr = self._pr(pr_number)
        if pr.state != "OPEN":
            raise CodeHostError(f"PR #{pr_number} is not open")
        pr.state = "MERGED"
        return MergeResult(merged=True, merger="fake", method=method)

    def create_release(self, *, tag: str, title: str, notes: str | None = None) -> str:
        self.releases.setdefault(tag, (title, notes))
        return f"fake://releases/{tag}"

    def ci_failure_summary(self, pr_number: int, *, max_chars: int = 8000) -> str:
        pr = self.prs.get(pr_number)
        return (pr.ci_failure if pr else "")[-max_chars:]

    def ref_ci_status(self, ref: str) -> str:
        return self.ref_status.get(ref, "unknown")

    def list_open_changes(self) -> list[OpenChange]:
        return [
            OpenChange(number=p.number, head_branch=p.branch, base_branch=p.base)
            for p in sorted(self.prs.values(), key=lambda p: p.number)
            if p.state == "OPEN"
        ]

    def _pr(self, n: int) -> FakePR:
        if n not in self.prs:
            raise CodeHostError(f"unknown PR #{n}")
        return self.prs[n]
