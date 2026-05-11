"""Issue-Triage als Pre-Phase im board-loop (Spec v0.4 Teil 6.3).

Ein Triager bekommt ein ``ReadyIssue`` und entscheidet, ob es noch
sinnvoll ist, einen vollen forge-Run dafür zu starten. Mögliche
Ergebnisse sind die ``TriageDecision``-Literals in
``forge_core.events.kinds.triage``: ``relevant``, ``stale``,
``duplicate``, ``already_solved``.

Architektur:

* ``IssueTriager`` ist ein Protocol — der board-loop hängt an dem ab,
  nicht an einer konkreten Klasse. Wer einen anderen LLM-Anbieter
  einbauen will, schreibt einen neuen Triager.
* ``LLMTriager`` ist die produktive Default-Impl: ein einziger
  Claude-CLI-Aufruf mit Read-only-Tools, der JSON zurückgibt.
* ``NoopTriager`` markiert alles als ``relevant`` — nützlich für
  Tests und für Spec-Setups, die Triage konfigurieren, aber temporär
  ausgeschaltet halten wollen.

Die GitHub-Side-Effects (Kommentar, Close) leben in
:mod:`forge_execute.triage.gh`, getrennt vom LLM-Aufruf — damit Tests
die Klassifikation isolieren können.
"""

from forge_execute.triage.base import (
    IssueTriager,
    NoopTriager,
    TriageError,
    TriageResult,
)
from forge_execute.triage.gh import close_issue, comment_issue
from forge_execute.triage.llm import LLMTriager

__all__ = [
    "IssueTriager",
    "LLMTriager",
    "NoopTriager",
    "TriageError",
    "TriageResult",
    "close_issue",
    "comment_issue",
]
