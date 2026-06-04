"""Best-effort Markdown-Parser für architect-Subagent-Pläne.

Spec v0.3 Teil 6.5: der Plan ist freier Markdown mit erwarteten — aber nicht
hart geforderten — Sektionen (Goal, Acceptance, Subtasks, Risk, Out of scope).
Dieser Parser ist defensiv: er extrahiert was er findet, und liefert sauber
Defaults wenn Sektionen fehlen.

Der Parser läuft NIEMALS auf User-Input — nur auf Subagent-Output. Trotzdem
defensiv geschrieben (kein eval, keine HTML-/Code-Execution).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "unknown"]
SubtaskStatus = Literal["done", "open", "failed", "unknown"]


@dataclass(frozen=True)
class ParsedSubtask:
    """Ein einzelner geplanter Schritt aus der `## Subtasks`-Sektion."""

    index: int
    """1-basierter Index, wie im Plan numeriert."""

    title: str
    """Kurztitel: erste fettgedruckte Phrase, sonst Text bis zum ersten
    Gedankenstrich-Trennzeichen."""

    status: SubtaskStatus = "unknown"
    """Best-effort aus einem Checkbox-Marker (`[x]`/`[ ]`/`[!]`) in der
    Zeile. `unknown`, wenn kein Marker gesetzt ist."""


@dataclass(frozen=True)
class ParsedPlan:
    """Best-effort-extrahierte Plan-Metadaten.

    Vollständiger Plan-Text bleibt im Blob-Store — hier nur Statistiken
    für das `PlanProposed`-Event-Payload.
    """

    subtask_count: int | None
    """Anzahl numerierter Items in `## Subtasks`. None wenn Sektion fehlt
    oder Plan unverständlich ist (z.B. Insufficient context)."""

    risk_level: RiskLevel
    """Erstes Wort der `## Risk`-Sektion, lowercase, gemappt auf
    low/medium/high/unknown."""

    out_of_scope: list[str]
    """Bullets aus `## Out of scope`, gestrippt."""

    insufficient_context: bool
    """True wenn der Plan-Header explizit 'Insufficient context' meldet."""

    subtasks: list[ParsedSubtask] = field(default_factory=list)
    """Strukturierte Schritte aus `## Subtasks`. Leer wenn Sektion fehlt.
    `subtask_count` bleibt die autoritative Zählung (= len, wenn parsbar)."""


def parse_plan(plan_md: str) -> ParsedPlan:
    """Parst einen architect-Plan-Markdown.

    Defensiv: leerer Input liefert Defaults, fehlende Sektionen liefern
    None/`unknown`/leere Liste statt Crash.
    """
    text = (plan_md or "").strip()
    if not text:
        return _empty()

    # `# Insufficient context` als Top-Header → architect verlangt Klarstellung
    if _matches_top_header(text, "insufficient context"):
        return ParsedPlan(
            subtask_count=None,
            risk_level="unknown",
            out_of_scope=[],
            insufficient_context=True,
        )

    subtasks = _parse_subtasks(text)
    return ParsedPlan(
        subtask_count=len(subtasks) if subtasks else _count_subtasks(text),
        risk_level=_extract_risk(text),
        out_of_scope=_extract_out_of_scope(text),
        insufficient_context=False,
        subtasks=subtasks,
    )


# --- Internals ----------------------------------------------------------


_RISK_LEVELS: tuple[RiskLevel, ...] = ("low", "medium", "high")


def _empty() -> ParsedPlan:
    return ParsedPlan(
        subtask_count=None,
        risk_level="unknown",
        out_of_scope=[],
        insufficient_context=False,
    )


def _matches_top_header(text: str, expected: str) -> bool:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return expected.lower() in line.lower()
        return False
    return False


def _section_body(text: str, section_name: str) -> str | None:
    """Extrahiert den Text-Body einer `## <section>`-Sektion (case-insensitive,
    optional `:` am Ende). Liefert None wenn Sektion fehlt."""
    # Pattern: ##... <name> (optional :), bis zum nächsten ## oder Datei-Ende
    pattern = rf"(?im)^\s*#{{2,}}\s*{re.escape(section_name)}\s*:?\s*$"
    match = re.search(pattern, text)
    if not match:
        return None
    start = match.end()
    # Suche nächsten ##-Header ab `start`
    next_header = re.search(r"(?m)^\s*#{2,}\s+\S", text[start:])
    end = start + next_header.start() if next_header else len(text)
    return text[start:end].strip()


def _count_subtasks(text: str) -> int | None:
    body = _section_body(text, "Subtasks")
    if body is None:
        return None
    # Numerierte items: "1." oder "1)" am Zeilenanfang (mit optionalen Spaces)
    pattern = re.compile(r"(?m)^\s*\d+[.)]\s+\S")
    return len(pattern.findall(body))


# Numeriertes Subtask-Item: führende Nummer + Rest der Zeile.
_SUBTASK_LINE = re.compile(r"(?m)^\s*(\d+)[.)]\s+(.+?)\s*$")
# Status-Checkbox irgendwo in der Zeile: [x] done, [ ] open, [!] failed.
_STATUS_BOX = re.compile(r"\[\s*([xX! ])\s*\]")
# Fettgedruckter Titel **...**.
_BOLD = re.compile(r"\*\*(.+?)\*\*")
# Trennzeichen, an denen wir den Titel abschneiden (em-dash, en-dash, " - ").
_TITLE_SPLIT = re.compile(r"\s+[—–]\s+|\s+-\s+")  # noqa: RUF001

_STATUS_MAP: dict[str, SubtaskStatus] = {
    "x": "done",
    "X": "done",
    "!": "failed",
    " ": "open",
    "": "open",
}


def _parse_subtasks(text: str) -> list[ParsedSubtask]:
    """Extrahiert strukturierte Subtasks aus `## Subtasks`.

    Eine Zeile pro Subtask (`N. ...`). Sub-Bullets (ohne führende Nummer)
    werden ignoriert. Defensiv: unparsbare Zeilen werden übersprungen, nicht
    geworfen.
    """
    body = _section_body(text, "Subtasks")
    if body is None:
        return []

    out: list[ParsedSubtask] = []
    for match in _SUBTASK_LINE.finditer(body):
        index = int(match.group(1))
        rest = match.group(2)

        status: SubtaskStatus = "unknown"
        box = _STATUS_BOX.search(rest)
        if box is not None:
            status = _STATUS_MAP.get(box.group(1), "unknown")
            rest = rest[: box.start()] + rest[box.end() :]

        title = _extract_title(rest)
        if title:
            out.append(ParsedSubtask(index=index, title=title, status=status))
    return out


def _extract_title(rest: str) -> str:
    """Titel aus dem Subtask-Zeilenrest: erst **bold**, sonst Text bis zum
    ersten Trennzeichen. Auf 120 Zeichen begrenzt."""
    bold = _BOLD.search(rest)
    if bold is not None:
        candidate = bold.group(1).strip()
    else:
        candidate = _TITLE_SPLIT.split(rest, maxsplit=1)[0]
    candidate = " ".join(candidate.split()).strip(" :-—–").strip()  # noqa: RUF001
    return candidate[:120]


def _extract_risk(text: str) -> RiskLevel:
    body = _section_body(text, "Risk")
    if body is None:
        return "unknown"
    # Erstes Wort lowercase nehmen
    head_word = body.lower().split(maxsplit=1)
    if not head_word:
        return "unknown"
    first = head_word[0].rstrip(":,.-")
    if first in _RISK_LEVELS:
        return first  # type: ignore[return-value]
    return "unknown"


def _extract_out_of_scope(text: str) -> list[str]:
    body = _section_body(text, "Out of scope")
    if body is None:
        return []
    items: list[str] = []
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith(("-", "*", "+")):
            content = stripped[1:].strip()
            if content:
                items.append(content)
    return items
