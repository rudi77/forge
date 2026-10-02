"""Anbieter-neutrale Datentypen für Work-Tracker und Code-Hosts.

forge spricht mit GitHub, Azure DevOps und (später) weiteren Anbietern. Die
Typen hier sind der gemeinsame Nenner, den ``forge-execute`` (Triage) und
``forge-cli`` (board-loop/Conductor) sehen — ohne von einem konkreten Adapter
abzuhängen. Die Protocols selbst (``WorkTracker``/``CodeHost``) leben in
``forge_adapters.base``; ``forge-core`` bleibt frei von Adapter-Imports.

IDs sind bei allen unterstützten Anbietern ganze Zahlen (GitHub-Issue/PR-Nummer,
Azure-Work-Item-/PR-ID). Darum bleiben ``issue_number``/``pr_number`` in den
Events gültig.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

ProviderName = Literal["github", "azure_devops"]

WorkItemKind = Literal["epic", "feature", "story", "bug", "task", "spec"]


@dataclass(frozen=True)
class ReadyIssue:
    """Ein Work-Item (GitHub-Issue, Azure-Work-Item, …) aus Sicht von forge.

    ``labels`` sind die Labels (GitHub) bzw. Tags (Azure DevOps); darüber laufen
    die ``forge:<stage>``-Stages. ``project_status`` wird für Telemetrie
    mitgeführt (Board-Status-Feld bzw. Work-Item-State).
    """

    number: int
    title: str
    body: str
    labels: list[str]
    project_status: str
    url: str
    kind: WorkItemKind | None = None
    """Work-Item-Typ, sofern der Anbieter ihn kennt (Azure: Work-Item-Type)."""

    parent: int | None = None
    """Eltern-Item (GitHub-Sub-Issue / Azure-Parent-Link), falls bekannt."""


@dataclass(frozen=True)
class NewWorkItem:
    """Ein von forge anzulegendes Work-Item (Abschnitt A der Roadmap)."""

    kind: WorkItemKind
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    parent: int | None = None


__all__ = ["NewWorkItem", "ProviderName", "ReadyIssue", "WorkItemKind"]
