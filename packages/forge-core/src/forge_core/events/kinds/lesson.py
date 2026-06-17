"""LessonLearned payload.

Eine kuratierte, destillierte Lektion, die der Master-Agent am Ende eines
orchestrierten Runs zurückmeldet (``---FORGE-LESSONS-...---``-Block). Anders als
Run-Outcomes oder die Failure-Heatmap ist eine Lektion **nicht** aus anderen
Events ableitbar — sie ist die qualitative Selbstauskunft des Agenten („dieses
Repo nutzt pytest-Marker X", „Migrationen brauchen Y zuerst"). Deshalb ein
eigenes Event und keine reine read-only-Derivation.

forge wertet diese Events read-only in den Projekt-Memory-Block aus (genau wie
Plan-Summaries und Run-Outcomes). Sie speisen NIE den Operator-Seed
``.forge/memory.md`` zurück: das bleibt die Operator-Datei, der Agent darf sie
nicht beschreiben (Forbidden Zone), und forge mutiert keinen State, den sie
sich selbst wieder als Kontext zurückreicht (Mantra 3). Das Gedächtnis lebt im
replay-fähigen Event-Strom, nicht in einer mutierbaren Datei.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forge_core.events.base import EventKind, register_payload

# Grobe Kategorie der Lektion — hält den Memory-Block sortierbar und erlaubt
# später gezieltes Filtern (z.B. nur `pitfall` an einen Reviewer geben).
LessonCategory = Literal[
    "pattern",  # wiederverwendbares Vorgehen / Architektur-Muster im Repo
    "pitfall",  # Stolperfalle, die Zeit/einen Fehlschlag gekostet hat
    "convention",  # Projekt-Konvention (Naming, Test-Layout, Tooling-Flags)
    "tooling",  # Build/Test/CI-spezifisches Wissen
    "general",  # alles andere
]

# Defensive Obergrenzen — die Lektion ist eine Ein-Zeilen-Destillation, kein
# Fließtext. Der Parser kürzt vorher; die Validierung ist die harte Grenze.
_MAX_LESSON_CHARS = 280
_MAX_FILES = 10


class LessonLearnedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lesson: str
    """Die destillierte Lektion als ein Satz/eine Zeile."""

    category: LessonCategory = "general"
    """Grobe Einordnung; default ``general`` wenn der Agent keine angab."""

    issue_number: int | None = None
    """Issue, in dessen Run die Lektion entstand (zur Nachverfolgung)."""

    files: list[str] = Field(default_factory=list)
    """Optionale, vom Agenten genannte relevante Datei-Pfade."""

    source: Literal["agent", "derived"] = "agent"
    """Herkunft. ``agent`` = Selbstauskunft des Master-Agenten (heute der
    einzige Pfad). ``derived`` ist reserviert für später automatisch aus dem
    Event-Strom verdichtete Lektionen."""

    @field_validator("lesson")
    @classmethod
    def _lesson_non_empty(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("lesson must not be empty")
        return text[:_MAX_LESSON_CHARS]

    @field_validator("files")
    @classmethod
    def _cap_files(cls, value: list[str]) -> list[str]:
        return [f.strip() for f in value if f.strip()][:_MAX_FILES]


register_payload(EventKind.LESSON_LEARNED, LessonLearnedPayload, "1.0")
