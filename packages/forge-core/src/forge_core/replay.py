"""Replay-API.

Aus der Spec Teil 4.4: jeder historische Run muss aus Event-Log + Blob-Store
deterministisch rekonstruierbar sein. Konkret heißt das, dass aus den Events
sich der ursprüngliche Prompt-Text, die Diff-Inhalte, die Eval-Outputs und die
verwendeten Tool-Versionen wiederherstellen lassen.

In v1 wird die Replay-Funktion noch nicht aktiv genutzt — aber die API muss
ab Tag eins so beschaffen sein, dass sie Replay ermöglicht. Das ist der
entscheidende Unterschied zu „Logging".

Diese Datei stellt die API in Skelett-Form bereit. Aktiv genutzt erst in v3.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forge_core.blobs import BlobStore
from forge_core.events import Event, EventKind
from forge_core.store import EventStore


@dataclass
class GenerationReconstruction:
    """Eine Generation mit aufgelösten Artefakt-Inhalten."""

    generation_id: str
    events: list[Event] = field(default_factory=list)
    artifacts: dict[str, bytes] = field(default_factory=dict)
    """Logische Artefakt-Schlüssel → Inhalt (von Hash aufgelöst)."""

    @property
    def proposal_prompt(self) -> str | None:
        """Convenience: Prompt-Text der ProposalRequested, falls vorhanden."""
        data = self.artifacts.get("prompt")
        return data.decode("utf-8") if data is not None else None

    @property
    def plan_md(self) -> str | None:
        """Convenience: Plan-Markdown vom architect-Subagent, falls vorhanden.

        Spec v0.3 Teil 6.1 — `PlanProposed`-Event hat `artifacts.plan` als
        CAS-Hash auf den Plan-Text.
        """
        data = self.artifacts.get("plan")
        return data.decode("utf-8") if data is not None else None

    @property
    def diff(self) -> str | None:
        data = self.artifacts.get("diff") or self.artifacts.get("structured_diff")
        return data.decode("utf-8") if data is not None else None

    @property
    def eval_stdout(self) -> str | None:
        data = self.artifacts.get("eval_stdout")
        return data.decode("utf-8") if data is not None else None


@dataclass
class RunReconstruction:
    """Vollständige Rekonstruktion eines Runs.

    Enthält alle Events in chronologischer Reihenfolge, gruppiert nach Generation,
    plus die Artefakt-Inhalte aus dem Blob-Store.
    """

    run_id: str
    events: list[Event] = field(default_factory=list)
    generations: list[GenerationReconstruction] = field(default_factory=list)
    run_level_artifacts: dict[str, bytes] = field(default_factory=dict)
    """Artefakte aus Run-Level-Events (RunStarted, RunFinished)."""

    @property
    def project(self) -> str | None:
        return self.events[0].project if self.events else None

    @property
    def factory_version(self) -> str | None:
        return self.events[0].factory_version if self.events else None

    @property
    def started_at(self) -> Any:
        for evt in self.events:
            if evt.kind is EventKind.RUN_STARTED:
                return evt.ts
        return None

    @property
    def finished_at(self) -> Any:
        for evt in reversed(self.events):
            if evt.kind is EventKind.RUN_FINISHED:
                return evt.ts
        return None


def reconstruct_run(
    run_id: str,
    *,
    store: EventStore,
    blobs: BlobStore | None = None,
) -> RunReconstruction:
    """Rekonstruiert einen Run aus Events + (optional) Blob-Store.

    Wenn `blobs` übergeben wird, werden die referenzierten Artefakt-Inhalte
    geladen. Fehlt ein Blob, bleibt der Eintrag in `artifacts` aus —
    Replay-Tests in v3 können daraus folgern, ob die Reproduktion vollständig
    war.
    """
    events = store.events_for_run(run_id)
    rec = RunReconstruction(run_id=run_id, events=events)

    by_generation: dict[str, GenerationReconstruction] = {}

    for evt in events:
        if evt.generation_id is not None:
            gen = by_generation.setdefault(
                evt.generation_id,
                GenerationReconstruction(generation_id=evt.generation_id),
            )
            gen.events.append(evt)
            if blobs is not None:
                _load_artifacts_into(gen.artifacts, evt.artifacts, blobs)
        else:
            if blobs is not None:
                _load_artifacts_into(rec.run_level_artifacts, evt.artifacts, blobs)

    # Reihenfolge: nach erstem Event-Timestamp pro Generation
    rec.generations = sorted(
        by_generation.values(),
        key=lambda g: g.events[0].ts if g.events else 0,
    )
    return rec


def _load_artifacts_into(
    target: dict[str, bytes],
    refs: dict[str, str],
    blobs: BlobStore,
) -> None:
    for key, hash_str in refs.items():
        if key in target:
            # Erstes Vorkommen gewinnt — bei wiederkehrenden Schlüsseln (z.B.
            # mehreren `prompt`-Artefakten in derselben Generation) nimmt
            # der Konsument das Event direkt ins Visier.
            continue
        try:
            target[key] = blobs.get(hash_str)
        except FileNotFoundError:
            # Blob fehlt — wird in v3 als Replay-Vollständigkeitssignal genutzt
            continue
