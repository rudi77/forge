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
from dataclasses import dataclass
from typing import Literal

RiskLevel = Literal["low", "medium", "high", "unknown"]


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

    return ParsedPlan(
        subtask_count=_count_subtasks(text),
        risk_level=_extract_risk(text),
        out_of_scope=_extract_out_of_scope(text),
        insufficient_context=False,
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
