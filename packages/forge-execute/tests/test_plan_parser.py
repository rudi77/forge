"""Tests für _plan_parser."""

from __future__ import annotations

from forge_execute._plan_parser import parse_plan


def test_full_plan_parses_all_fields() -> None:
    md = """# Plan: Logo im PDF rendern

## Goal
User-Logo soll oben links im Quote-PDF erscheinen.

## Acceptance
test_pdf_embeds_logo_when_logo_path_set ist grün.

## Existing patterns I found
- `_render_pdf` in `tools/generate_quote_pdf.py:104`
- `Image()` aus reportlab.platypus

## Design decisions
1. Logo via `quote['company']['logo_path']` reichen
2. `Image(path, width=40*mm, height=20*mm)`

## Subtasks
1. **Image-Import** — file: `generate_quote_pdf.py` — change: ergänze Image — verified by: pytest
2. **Logo-Block in _render_pdf** — file: `generate_quote_pdf.py` — change: vor letterhead — verified by: test_pdf_embeds_logo
3. **Defensive logo_path-Check** — file: `generate_quote_pdf.py` — change: try/except — verified by: test_pdf_omits_logo_when_logo_path_invalid

## Out of scope
- Logo-Größe konfigurierbar machen
- PDF/A-Konformität

## Risk
low — lokale Änderung in einer Datei, gute Test-Abdeckung.
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 3
    assert parsed.risk_level == "low"
    assert parsed.out_of_scope == [
        "Logo-Größe konfigurierbar machen",
        "PDF/A-Konformität",
    ]
    assert parsed.insufficient_context is False


def test_insufficient_context_detected() -> None:
    md = """# Insufficient context

I need clarification on:
1. Welche PDF-Library?
2. Logo-Format?
"""
    parsed = parse_plan(md)
    assert parsed.insufficient_context is True
    assert parsed.subtask_count is None
    assert parsed.risk_level == "unknown"


def test_empty_or_whitespace_returns_defaults() -> None:
    for inp in ["", "   ", "\n\n\n"]:
        parsed = parse_plan(inp)
        assert parsed.subtask_count is None
        assert parsed.risk_level == "unknown"
        assert parsed.out_of_scope == []
        assert parsed.insufficient_context is False


def test_missing_sections_dont_crash() -> None:
    md = """# Plan

Just some prose, no sections.
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count is None
    assert parsed.risk_level == "unknown"
    assert parsed.out_of_scope == []


def test_risk_levels() -> None:
    for level, expected in [
        ("low", "low"),
        ("LOW", "low"),
        ("medium - some refactor", "medium"),
        ("HIGH risk: touches auth", "high"),
        ("complete chaos", "unknown"),
    ]:
        md = f"# Plan\n## Risk\n{level}\n"
        assert parse_plan(md).risk_level == expected


def test_subtask_count_handles_variations() -> None:
    md = """# Plan
## Subtasks
1. First
2. Second
3) Third (paren-style)
"""
    assert parse_plan(md).subtask_count == 3


def test_out_of_scope_with_dash_and_asterisk() -> None:
    md = """# Plan
## Out of scope
- Item one
* Item two
- Item three
"""
    parsed = parse_plan(md)
    assert parsed.out_of_scope == ["Item one", "Item two", "Item three"]


def test_section_header_with_colon() -> None:
    """LLM darf abweichen — Header kann mit `:` enden."""
    md = """# Plan
## Subtasks:
1. foo
2. bar
## Risk:
medium
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 2
    assert parsed.risk_level == "medium"


def test_section_order_irrelevant() -> None:
    md = """# Plan
## Risk
high
## Subtasks
1. one
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 1
    assert parsed.risk_level == "high"


def test_zero_subtasks() -> None:
    """Plan mit leerer Subtasks-Sektion → count=0, nicht None."""
    md = """# Plan
## Subtasks

No subtasks needed.

## Risk
low
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 0
    assert parsed.risk_level == "low"


def test_subtasks_structured_title_and_index() -> None:
    md = """# Plan
## Subtasks
1. **Image-Import** — file: `x.py` — change: ergänze Image — verified by: pytest
2. **Logo-Block** — file: `x.py` — change: vor letterhead — verified by: test
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 2
    assert [s.index for s in parsed.subtasks] == [1, 2]
    assert [s.title for s in parsed.subtasks] == ["Image-Import", "Logo-Block"]
    # Ohne Status-Marker → unknown.
    assert all(s.status == "unknown" for s in parsed.subtasks)


def test_subtasks_title_fallback_without_bold() -> None:
    """Ohne **bold** wird der Text bis zum ersten Trennzeichen genommen."""
    md = """# Plan
## Subtasks
1. Add import — file: `x.py`
2) Plain title with no separator
"""
    parsed = parse_plan(md)
    assert parsed.subtasks[0].title == "Add import"
    assert parsed.subtasks[1].title == "Plain title with no separator"


def test_subtasks_status_checkboxes() -> None:
    md = """# Plan
## Subtasks
1. [x] **Done one** — file: `x.py`
2. [ ] **Open two** — file: `y.py`
3. [!] **Failed three** — file: `z.py`
"""
    parsed = parse_plan(md)
    assert [s.status for s in parsed.subtasks] == ["done", "open", "failed"]
    # Checkbox wird aus dem Titel entfernt.
    assert [s.title for s in parsed.subtasks] == [
        "Done one",
        "Open two",
        "Failed three",
    ]


def test_subtask_count_matches_structured_len() -> None:
    """subtask_count folgt der strukturierten Liste, wenn parsbar."""
    md = """# Plan
## Subtasks
1. **A** — x
2. **B** — y
3. **C** — z
"""
    parsed = parse_plan(md)
    assert parsed.subtask_count == 3
    assert len(parsed.subtasks) == 3


def test_insufficient_context_has_no_subtasks() -> None:
    md = "# Insufficient context\n\n1. Welche Library?\n"
    parsed = parse_plan(md)
    assert parsed.subtasks == []
