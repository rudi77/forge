"""Tests für den ``---FORGE-LESSONS-...---``-Block-Parser."""

from __future__ import annotations

from forge_execute._lesson_parser import parse_lessons


def test_parse_lessons_empty_or_none() -> None:
    assert parse_lessons(None) == []
    assert parse_lessons("") == []
    assert parse_lessons("   \n  \n") == []


def test_parse_lessons_plain_lines() -> None:
    block = "Use the existing _emit helper.\nTests live without __init__.py."
    out = parse_lessons(block)
    assert [le.lesson for le in out] == [
        "Use the existing _emit helper.",
        "Tests live without __init__.py.",
    ]
    assert all(le.category == "general" for le in out)
    assert all(le.files == [] for le in out)


def test_parse_lessons_category_tag_and_files() -> None:
    block = "- [pitfall] DuckDB takes one file lock per process (files: store.py, x.py)"
    out = parse_lessons(block)
    assert len(out) == 1
    le = out[0]
    assert le.category == "pitfall"
    assert le.lesson == "DuckDB takes one file lock per process"
    assert le.files == ["store.py", "x.py"]


def test_parse_lessons_unknown_category_falls_back_to_general() -> None:
    out = parse_lessons("[wat] something")
    assert out[0].category == "general"
    # Unbekannter Tag wird NICHT abgestreift, bleibt Teil des Textes.
    assert out[0].lesson == "[wat] something"


def test_parse_lessons_strips_list_markers() -> None:
    out = parse_lessons("- one\n* two\n3. three")
    assert [le.lesson for le in out] == ["one", "two", "three"]


def test_parse_lessons_dedupes_case_insensitive() -> None:
    out = parse_lessons("Same lesson\nsame lesson\nSAME LESSON")
    assert len(out) == 1


def test_parse_lessons_caps_count() -> None:
    block = "\n".join(f"lesson number {i}" for i in range(50))
    out = parse_lessons(block)
    assert len(out) == 10  # _MAX_LESSONS


def test_parse_lessons_truncates_long_lesson() -> None:
    long = "x" * 500
    out = parse_lessons(long)
    assert len(out[0].lesson) == 280  # _MAX_LESSON_CHARS
