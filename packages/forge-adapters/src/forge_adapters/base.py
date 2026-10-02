"""Anbieter-Schnittstellen: ``WorkTracker`` + ``CodeHost``.

forge trennt zwei Rollen, weil sie in der Praxis auseinanderfallen (z.B.
Azure Boards als Tracker + GitHub-Repos als Code-Host):

* :class:`WorkTracker` — Work-Items (Issues/Work Items): lesen, Stage-Labels
  setzen, kommentieren, schließen, anlegen.
* :class:`CodeHost` — Change-Requests (PRs): Branch pushen, PR öffnen,
  Review posten, CI-Status lesen, mergen, Releases anlegen.

``forge-cli`` programmiert ausschließlich gegen diese Protocols; die konkreten
Implementierungen liegen in ``forge_adapters.github`` / ``forge_adapters.azure``
und werden über :mod:`forge_adapters.registry` aus der Spec gebaut. Ein neuer
Anbieter ist fertig, wenn er die Vertrags-Testsuite
(``tests/test_provider_contract.py``) besteht.

Fehler: jede Implementierung wirft :class:`TrackerError` bzw.
:class:`CodeHostError` (oder eine Unterklasse) — der Conductor fängt genau
diese, unabhängig vom Anbieter.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from forge_core.spec import BoardConfig
from forge_core.tracking import NewWorkItem, ReadyIssue

MergeMethod = Literal["squash", "merge", "rebase"]
CloseReason = Literal["completed", "not planned"]
CiStatus = Literal["pass", "fail", "pending", "none", "unknown"]


class TrackerError(RuntimeError):
    """Work-Tracker-Aufruf fehlgeschlagen (CLI-Fehler, Rechte, ungültige Antwort)."""


class CodeHostError(RuntimeError):
    """Code-Host-Aufruf fehlgeschlagen (push, PR, Review, Merge, Release)."""


@dataclass(frozen=True)
class PRCreationResult:
    pr_number: int
    url: str
    branch: str


@dataclass(frozen=True)
class PRMetadata:
    """Schlanke, anbieter-neutrale Sicht auf einen offenen Change-Request."""

    number: int
    title: str
    body: str
    state: str
    """``OPEN`` | ``CLOSED`` | ``MERGED`` (normalisiert, uppercase)."""
    base_branch: str
    head_branch: str
    ci_status: str
    """pass|fail|pending|none|unknown."""
    mergeable: str
    """``MERGEABLE`` | ``CONFLICTING`` | ``UNKNOWN``."""


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    merger: str
    method: str


@dataclass(frozen=True)
class OpenChange:
    """Ein offener Change-Request (für das Nachziehen von Geschwister-PRs, G)."""

    number: int
    head_branch: str
    base_branch: str


@dataclass(frozen=True)
class LabelReport:
    """Ergebnis von :meth:`WorkTracker.ensure_labels`."""

    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    created: list[str] = field(default_factory=list)


def git_push_argv(*, branch: str, remote: str = "origin", target: str | None = None) -> list[str]:
    """argv für einen forge-Push. Zwei harte Leitplanken, anbieter-neutral:

    * nie ``--force`` (``push_force`` ist kategorisch verboten),
    * ein ``target`` (Push auf fremden Branch-Namen) nur auf ``forge/*`` —
      forge schreibt nie in menschliche Branches oder ``main``.
    """
    if target is None:
        return ["git", "push", "-u", remote, branch]
    if not target.startswith("forge/"):
        raise CodeHostError(
            f"refusing to push onto non-forge branch {target!r} "
            "(only forge/* branches may be updated by forge)"
        )
    return ["git", "push", remote, f"{branch}:refs/heads/{target}"]


@runtime_checkable
class WorkTracker(Protocol):
    """Work-Items lesen und schreiben (Issues, Work Items)."""

    provider: str

    def list_ready_items(self, board: BoardConfig) -> list[ReadyIssue]:
        """Board-ready Items (Status-Filter + Label-Filter + Idempotenz)."""
        ...

    def list_stage_items(
        self, *, stage_labels: list[str], state: str = "open"
    ) -> list[ReadyIssue]:
        """Items, die mindestens EIN Label aus ``stage_labels`` tragen.

        ``state``: ``open`` | ``closed`` | ``all``. Sortiert nach Nummer."""
        ...

    def get_items(self, numbers: list[int]) -> list[ReadyIssue]:
        """Items per Nummer laden (``--issue``-Override)."""
        ...

    def set_stage(self, *, number: int, add: str, remove: str | None = None) -> None:
        """Stage-Übergang: ``add`` setzen, ``remove`` entfernen. Idempotent."""
        ...

    def comment(self, *, number: int, body: str) -> None:
        """Kommentar an ein Item."""
        ...

    def close(self, *, number: int, reason: CloseReason = "not planned") -> None:
        """Item schließen."""
        ...

    def ensure_labels(self, labels: list[str], *, create: bool = False) -> LabelReport:
        """Prüft, ob ``labels`` im Tracker existieren; legt fehlende bei
        ``create=True`` an (GitHub-Labels; bei Tag-basierten Trackern wie
        Azure DevOps existiert ein Tag implizit, sobald er benutzt wird)."""
        ...

    def create_item(self, item: NewWorkItem) -> ReadyIssue:
        """Legt ein Work-Item an und liefert es (mit Nummer + URL) zurück."""
        ...

    def search_items(self, text: str) -> list[ReadyIssue]:
        """Items, deren Body ``text`` enthält (für Fingerprint-Dedupe, A3)."""
        ...


@runtime_checkable
class CodeHost(Protocol):
    """Change-Requests (PRs) und Releases."""

    provider: str

    def push_branch(
        self, *, branch: str, remote: str = "origin", target: str | None = None
    ) -> None:
        """``git push -u <remote> <branch>`` — nie ``--force``.

        ``target`` gesetzt → ``git push <remote> <branch>:refs/heads/<target>``
        (Nacharbeit auf einem bestehenden PR-Branch). git lehnt einen
        Nicht-Fast-Forward ab; forge erzwingt nie."""
        ...

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
        ...

    def queue_auto_merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> None:
        """Server-seitiges Auto-Merge (GitHub ``--auto``, Azure auto-complete)."""
        ...

    def fetch_metadata(self, pr_number: int) -> PRMetadata:
        ...

    def fetch_diff(self, pr_number: int) -> str:
        ...

    def head_committed_at(self, pr_number: int) -> datetime | None:
        """Zeitstempel des Head-Commits. Fail-open: ``None`` bei jedem Fehler."""
        ...

    def post_review(self, *, pr_number: int, approve: bool, body: str) -> None:
        ...

    def merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> MergeResult:
        ...

    def create_release(self, *, tag: str, title: str, notes: str | None = None) -> str:
        """Tag + Release anlegen (idempotent). Liefert eine URL (oder ``""``)."""
        ...

    def ci_failure_summary(self, pr_number: int, *, max_chars: int = 8000) -> str:
        """Namen der roten Checks + gekürzter Log-Auszug. Fail-open: ``""``."""
        ...

    def ref_ci_status(self, ref: str) -> str:
        """CI-Status eines Branches (z.B. ``main``). Fail-open: ``unknown``."""
        ...

    def list_open_changes(self) -> list[OpenChange]:
        """Offene Change-Requests (für das Nachziehen nach einem Merge)."""
        ...


__all__ = [
    "CiStatus",
    "CloseReason",
    "CodeHost",
    "CodeHostError",
    "LabelReport",
    "MergeMethod",
    "MergeResult",
    "NewWorkItem",
    "OpenChange",
    "PRCreationResult",
    "PRMetadata",
    "ReadyIssue",
    "TrackerError",
    "WorkTracker",
    "git_push_argv",
]
