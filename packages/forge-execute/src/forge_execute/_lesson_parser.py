"""Best-effort-Parser für den ``---FORGE-LESSONS-...---``-Block.

Der Master-Agent meldet am Run-Ende optional kuratierte Lektionen, je eine pro
Zeile. Format pro Zeile (alles außer dem Satz optional):

    - [pitfall] DuckDB nimmt einen Datei-Lock pro Prozess (files: store.py)
    [convention] Tests liegen ohne __init__.py in tests/
    Reuse the existing _emit helper instead of build_event directly.

Dieser Parser läuft NUR auf Subagent-Output, nie auf User-Input — trotzdem
defensiv (kein eval, keine Code-Execution). Er extrahiert was er findet und
liefert saubere Defaults; eine unparsbare Zeile wird als ``general``-Lektion
ohne Files übernommen, solange Text übrig bleibt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

LessonCategory = Literal["pattern", "pitfall", "convention", "tooling", "general"]

# Spiegelt die Kategorien aus forge_core.events.kinds.lesson. Bewusst hier
# dupliziert (forge-execute importiert nicht das Literal aus core für eine
# Laufzeit-Menge) — eine kleine, stabile Liste.
_KNOWN_CATEGORIES: frozenset[str] = frozenset(
    {"pattern", "pitfall", "convention", "tooling", "general"}
)

# Obergrenzen — defensiv gegen einen Agenten, der einen Roman in den Block
# schreibt. Das Event-Schema kürzt die Lektion zusätzlich hart.
_MAX_LESSONS = 10
_MAX_LESSON_CHARS = 280
_MAX_FILES = 10

_CATEGORY_RE = re.compile(r"^\[\s*([A-Za-z]+)\s*\]\s*")
_FILES_RE = re.compile(r"\(\s*files?\s*:\s*([^)]*)\)\s*$", re.IGNORECASE)


@dataclass(frozen=True)
class ParsedLesson:
    """Eine destillierte Lektion aus dem Lessons-Block."""

    lesson: str
    """Der Lektions-Satz, ohne Kategorie-Tag/Files-Suffix, gekürzt."""

    category: LessonCategory = "general"
    """Aus einem führenden ``[tag]`` gemappt; ``general`` wenn keiner/unbekannt."""

    files: list[str] = field(default_factory=list)
    """Pfade aus einem abschließenden ``(files: a, b)``-Suffix."""


def parse_lessons(block: str | None) -> list[ParsedLesson]:
    """Zerlegt den rohen Block-Text in strukturierte Lektionen.

    Leerer/None-Block → leere Liste. Höchstens ``_MAX_LESSONS`` Lektionen,
    Duplikate (case-insensitive nach dem Lektions-Text) werden zusammengefasst.
    """
    if not block:
        return []

    out: list[ParsedLesson] = []
    seen: set[str] = set()
    for raw in block.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Listen-Marker abstreifen (`- `, `* `, `1. `).
        line = re.sub(r"^(?:[-*]|\d+\.)\s+", "", line).strip()
        if not line:
            continue

        category: str = "general"
        m = _CATEGORY_RE.match(line)
        if m:
            tag = m.group(1).lower()
            # Nur einen bekannten Tag als Kategorie abstreifen. Ein unbekanntes
            # ``[wort]`` bleibt Teil des Lektions-Textes (könnte echter Inhalt
            # sein, kein Kategorie-Tag).
            if tag in _KNOWN_CATEGORIES:
                category = tag
                line = line[m.end() :].strip()

        files: list[str] = []
        fm = _FILES_RE.search(line)
        if fm:
            files = [
                f.strip()
                for f in re.split(r"[,\s]+", fm.group(1))
                if f.strip()
            ][:_MAX_FILES]
            line = line[: fm.start()].strip()

        if not line:
            continue
        text = line[:_MAX_LESSON_CHARS]
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            ParsedLesson(
                lesson=text,
                category=category,  # type: ignore[arg-type]
                files=files,
            )
        )
        if len(out) >= _MAX_LESSONS:
            break
    return out
