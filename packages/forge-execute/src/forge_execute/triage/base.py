"""Protocol + Datentypen + Noop-Impl für den Triage-Layer."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from forge_adapters.github import ReadyIssue
from forge_core.events import TriageDecision


class TriageError(RuntimeError):
    """Triager konnte keine valide Entscheidung produzieren.

    Beispiele: claude-Subprozess crasht, JSON-Output ist kein gültiges
    ``TriageDecision``, Wallclock-Budget überschritten. Der Caller
    (board-loop) behandelt das als "im Zweifel relevant" und dispatched
    den Run regulär — Triage darf den Hauptpfad nicht blockieren.
    """


@dataclass(frozen=True)
class TriageResult:
    """Strukturiertes Ergebnis eines Triage-Aufrufs.

    ``decision`` ist Pflicht; der Rest informativ. ``cost_usd`` wird
    vom Caller in die Run-Telemetrie eingerechnet, sodass spätere
    Auswertungen sehen, ob Triage sich gegenüber den vermiedenen
    Volldispatches rechnet.
    """

    decision: TriageDecision
    reason: str = ""
    related_pr: int | None = None
    related_commit: str | None = None
    turns_used: int = 0
    cost_usd: Decimal = Decimal("0")
    model: str | None = None

    @property
    def is_relevant(self) -> bool:
        return self.decision == "relevant"


class IssueTriager(Protocol):
    """Was der board-loop von einem Triager erwartet.

    Sync — board-loop ist sequential. Wallclock-Budget-Cap und
    Tool-Restriktionen sind Sache der Impl.
    """

    def triage(self, *, issue: ReadyIssue, repo_root: Path) -> TriageResult:
        """Klassifiziert das Issue. Bei Fehlern: TriageError werfen."""
        ...


class NoopTriager:
    """Liefert ``relevant`` für alles, ohne Kosten.

    Nützlich für Tests und als Fallback, wenn ``triage.enabled`` an,
    aber das Modell-Setup nicht verfügbar ist (z.B. claude-Binary fehlt
    auf einem CI-Runner, der nur das Board-Listing testen will).
    """

    def triage(self, *, issue: ReadyIssue, repo_root: Path) -> TriageResult:
        return TriageResult(decision="relevant", reason="noop triager")
