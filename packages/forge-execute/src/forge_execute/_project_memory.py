"""Project-wide memory across forge runs.

Builds a bounded Markdown context from an optional operator seed
(``.forge/memory.md``) plus recent ``PlanProposed`` artefacts from the
EventStore. Injected into every proposal prompt and recorded as a CAS
artefact (``project_memory``) for replay.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from forge_core.blobs import BlobStore
from forge_core.store import EventStore

from forge_execute._plan_parser import parse_plan

_MAX_TOTAL_CHARS = 8000
_MAX_PLAN_SNIPPET = 1500
_MAX_RECENT_PLANS = 3
_MAX_RECENT_OUTCOMES = 5
_MAX_FOCUS_CHARS = 100
_SEED_FILENAME = "memory.md"

# Gedächtnis-Erweiterungen (A/B/C). Bewusst kleine Caps — der Memory-Block
# konkurriert mit dem Task-Kontext um Tokens; _truncate ist die harte Grenze.
_MAX_LESSONS = 8
_MAX_FAILURE_MODES = 5
_MAX_HOTSPOTS = 8
_HOTSPOT_SCAN = 200  # wie viele jüngste ProposalReceived-Events durchgezählt werden
_MIN_HOTSPOT_HITS = 2  # erst ab 2 Runs gilt eine Datei als „häufig angefasst"


def load_memory_seed(forge_dir: Path) -> str | None:
    """Reads ``<forge_dir>/memory.md`` when present."""
    path = forge_dir / _SEED_FILENAME
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def collect_recent_plan_summaries(
    store: EventStore,
    blobs: BlobStore,
    *,
    project: str,
    exclude_run_id: str | None = None,
    limit: int = _MAX_RECENT_PLANS,
) -> list[tuple[str, str]]:
    """Returns ``(run_id, summary_md)`` for recent successful plans."""
    rows = store.query(
        """
        SELECT run_id, artifacts
        FROM events
        WHERE kind = 'PlanProposed'
          AND project = ?
          AND COALESCE(
                json_extract_string(payload, '$.insufficient_context'),
                'false'
              ) != 'true'
          AND (? IS NULL OR run_id != ?)
        ORDER BY ts DESC, event_id DESC
        LIMIT ?
        """,
        [project, exclude_run_id, exclude_run_id, limit],
    )
    out: list[tuple[str, str]] = []
    for row in rows:
        run_id = str(row.get("run_id") or "")
        artifacts_raw = row.get("artifacts")
        if not artifacts_raw:
            continue
        if isinstance(artifacts_raw, str):
            try:
                artifacts = json.loads(artifacts_raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(artifacts_raw, dict):
            artifacts = artifacts_raw
        else:
            continue
        plan_hash = artifacts.get("plan")
        if not isinstance(plan_hash, str) or not blobs.has(plan_hash):
            continue
        plan_md = blobs.get_text(plan_hash)
        summary = _summarize_plan(plan_md, run_id=run_id)
        if summary:
            out.append((run_id, summary))
    return out


def collect_recent_run_outcomes(
    store: EventStore,
    *,
    project: str,
    exclude_run_id: str | None = None,
    limit: int = _MAX_RECENT_OUTCOMES,
) -> list[str]:
    """Eine Zusammenfassungs-Zeile pro kürzlich beendetem Run.

    Korreliert rein aus dem Event-Strom: ``RunStarted`` (issue/focus) +
    ``RunFinished`` (decision/PR) + ``PRMerged``, verknüpft über ``run_id``.
    Damit veraltet der Erledigt-Status nie — auch wenn die Operator-Seed-
    Prosa in ``memory.md`` („WP4 noch offen") nicht nachgepflegt wird,
    sieht der nächste Run hier, was bereits gebaut und gemerged wurde.
    """
    rows = store.query(
        """
        SELECT f.run_id AS run_id,
               json_extract_string(s.payload, '$.issue_number') AS issue_number,
               json_extract_string(s.payload, '$.focus') AS focus,
               json_extract_string(f.payload, '$.decision') AS decision,
               json_extract_string(f.payload, '$.pr_number') AS pr_number,
               EXISTS (
                   SELECT 1 FROM events m
                   WHERE m.run_id = f.run_id AND m.kind = 'PRMerged'
               ) AS merged
        FROM events f
        JOIN events s ON s.run_id = f.run_id AND s.kind = 'RunStarted'
        WHERE f.kind = 'RunFinished'
          AND f.project = ?
          AND (? IS NULL OR f.run_id != ?)
        ORDER BY f.ts DESC, f.event_id DESC
        LIMIT ?
        """,
        [project, exclude_run_id, exclude_run_id, limit],
    )
    out: list[str] = []
    for row in rows:
        run_id = str(row.get("run_id") or "")
        head = [f"run `{run_id[:8]}`"]
        issue = row.get("issue_number")
        if issue not in (None, ""):
            head.append(f"issue #{issue}")
        decision = str(row.get("decision") or "unknown")
        line = f"- {' — '.join(head)}: **{decision}**"
        pr = row.get("pr_number")
        if pr not in (None, ""):
            line += f" (PR #{pr}{', merged' if row.get('merged') else ''})"
        focus = str(row.get("focus") or "").strip()
        if focus:
            line += f" — {_one_line(focus)[:_MAX_FOCUS_CHARS]}"
        out.append(line)
    return out


def collect_recent_lessons(
    store: EventStore,
    *,
    project: str,
    exclude_run_id: str | None = None,
    limit: int = _MAX_LESSONS,
) -> list[str]:
    """Kuratierte Lektionen aus ``LessonLearned``-Events (jüngste zuerst).

    Das ist die ein einzige Quelle, die NICHT aus anderen Events ableitbar ist
    (Selbstauskunft des Agenten). Dedupliziert über den Lektions-Text, damit
    derselbe Hinweis aus mehreren Runs den Block nicht flutet.
    """
    rows = store.query(
        """
        SELECT json_extract_string(payload, '$.lesson') AS lesson,
               json_extract_string(payload, '$.category') AS category
        FROM events
        WHERE kind = 'LessonLearned'
          AND project = ?
          AND (? IS NULL OR run_id != ?)
        ORDER BY ts DESC, event_id DESC
        LIMIT ?
        """,
        [project, exclude_run_id, exclude_run_id, limit * 4],
    )
    out: list[str] = []
    seen: set[str] = set()
    for row in rows:
        lesson = str(row.get("lesson") or "").strip()
        if not lesson:
            continue
        key = lesson.lower()
        if key in seen:
            continue
        seen.add(key)
        category = str(row.get("category") or "general").strip()
        if category and category != "general":
            out.append(f"- [{category}] {lesson}")
        else:
            out.append(f"- {lesson}")
        if len(out) >= limit:
            break
    return out


def collect_recent_failures(
    store: EventStore,
    *,
    project: str,
    exclude_run_id: str | None = None,
    limit: int = _MAX_FAILURE_MODES,
) -> list[str]:
    """Wiederkehrende Fehlschläge, rein aus dem Event-Strom abgeleitet.

    Gruppiert explizit gescheiterte Events (``success = FALSE``) nach Kind +
    ``error_class`` und zählt sie. Der nächste Run sieht so, in welche
    Sackgassen frühere Runs gelaufen sind (Timeouts, Agent-Fehler,
    Guardrail-Verletzungen) — Failure-Memory ohne neues Event (Mantra 3).
    """
    rows = store.query(
        """
        SELECT kind AS kind,
               COALESCE(error_class, '(unspecified)') AS error_class,
               COUNT(*) AS n
        FROM events
        WHERE project = ?
          AND success = FALSE
          AND (? IS NULL OR run_id != ?)
        GROUP BY kind, error_class
        ORDER BY n DESC, kind
        LIMIT ?
        """,
        [project, exclude_run_id, exclude_run_id, limit],
    )
    out: list[str] = []
    for row in rows:
        n = int(row.get("n") or 0)
        if n <= 0:
            continue
        error_class = str(row.get("error_class") or "(unspecified)")
        kind = str(row.get("kind") or "?")
        unit = "time" if n == 1 else "times"
        out.append(f"- `{error_class}` during {kind} ({n} {unit})")
    return out


def collect_file_hotspots(
    store: EventStore,
    *,
    project: str,
    exclude_run_id: str | None = None,
    limit: int = _MAX_HOTSPOTS,
) -> list[str]:
    """Häufig angefasste Dateien, aggregiert aus ``ProposalReceived``.

    Zählt die ``files_touched`` der jüngsten Proposals und liefert die Dateien,
    die über mindestens ``_MIN_HOTSPOT_HITS`` Proposals hinweg auftauchen. Gibt
    dem nächsten Run (und via Context-Handoff den Subagents) einen Hinweis,
    welche Pfade für dieses Projekt zentral sind — spart kaltes Re-Scannen.
    """
    rows = store.query(
        """
        SELECT payload
        FROM events
        WHERE kind = 'ProposalReceived'
          AND project = ?
          AND (? IS NULL OR run_id != ?)
        ORDER BY ts DESC, event_id DESC
        LIMIT ?
        """,
        [project, exclude_run_id, exclude_run_id, _HOTSPOT_SCAN],
    )
    counts: Counter[str] = Counter()
    for row in rows:
        payload_raw = row.get("payload")
        if isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                continue
        elif isinstance(payload_raw, dict):
            payload = payload_raw
        else:
            continue
        for f in payload.get("files_touched") or []:
            if isinstance(f, str) and f.strip():
                counts[f.strip()] += 1
    out: list[str] = []
    for path, n in counts.most_common(limit):
        if n < _MIN_HOTSPOT_HITS:
            break  # most_common ist absteigend — ab hier nur noch seltenere
        out.append(f"- `{path}` ({n} proposals)")
    return out


def build_project_memory(
    *,
    forge_dir: Path,
    store: EventStore,
    blobs: BlobStore,
    project: str,
    exclude_run_id: str | None = None,
) -> str:
    """Assembles deterministic project memory Markdown (may be empty)."""
    parts: list[str] = []

    seed = load_memory_seed(forge_dir)
    if seed:
        parts.append("## Operator seed\n")
        parts.append(seed)

    lessons = collect_recent_lessons(
        store,
        project=project,
        exclude_run_id=exclude_run_id,
    )
    if lessons:
        parts.append("## Lessons learned\n")
        parts.append(
            "Distilled by earlier runs on this repo — conventions, pitfalls, "
            "and reusable patterns. Apply them; do not relearn them.\n"
        )
        parts.extend(lessons)
        parts.append("")

    outcomes = collect_recent_run_outcomes(
        store,
        project=project,
        exclude_run_id=exclude_run_id,
    )
    if outcomes:
        parts.append("## Recent run outcomes\n")
        parts.append(
            "Completion status from the event stream — current even when "
            "the operator seed is stale. A workpaket listed here with "
            "`pr_created` is already implemented; do not re-plan it.\n"
        )
        parts.extend(outcomes)
        parts.append("")

    failures = collect_recent_failures(
        store,
        project=project,
        exclude_run_id=exclude_run_id,
    )
    if failures:
        parts.append("## Recurring failures & dead-ends\n")
        parts.append(
            "Failure modes seen across prior runs. Anticipate these — a "
            "repeating timeout or agent error often means a step needs a "
            "different approach, not a retry.\n"
        )
        parts.extend(failures)
        parts.append("")

    hotspots = collect_file_hotspots(
        store,
        project=project,
        exclude_run_id=exclude_run_id,
    )
    if hotspots:
        parts.append("## Frequently touched files\n")
        parts.append(
            "Files changed across multiple prior proposals — likely central "
            "to this project. Start here instead of re-scanning the repo.\n"
        )
        parts.extend(hotspots)
        parts.append("")

    recent = collect_recent_plan_summaries(
        store,
        blobs,
        project=project,
        exclude_run_id=exclude_run_id,
    )
    if recent:
        parts.append("## Recent successful plans\n")
        for _run_id, summary in recent:
            parts.append(summary)
            parts.append("")

    if not parts:
        return ""

    body = "\n".join(parts).strip()
    return _truncate(body, _MAX_TOTAL_CHARS)


def render_project_memory_addendum(memory_md: str) -> str:
    """Wraps memory for injection into the proposal prompt."""
    if not memory_md.strip():
        return ""
    return (
        "\n---\n"
        "## forge project memory — orientation from prior runs\n\n"
        "Use this to avoid re-discovering stable project facts. "
        "Still read task-specific Spec sections and acceptance criteria "
        "for the current workpaket. When the operator seed and the run "
        "outcomes disagree about what is already done, the run outcomes "
        "win — they come from the event stream and cannot go stale.\n\n"
        f"{memory_md}\n"
    )


def _summarize_plan(plan_md: str, *, run_id: str) -> str:
    """Compresses a plan to goal, patterns, and subtask titles."""
    parsed = parse_plan(plan_md)
    if parsed.insufficient_context:
        return ""

    short_run = run_id[:8] if len(run_id) >= 8 else run_id
    lines = [f"### Prior plan (run `{short_run}`)"]

    goal = _extract_section(plan_md, "Goal")
    if goal:
        lines.append(f"**Goal:** {_one_line(goal)}")

    patterns = _extract_section(plan_md, "Existing patterns I found")
    if patterns:
        lines.append("**Patterns:**")
        for bullet in _bullets(patterns)[:4]:
            lines.append(f"- {bullet}")

    if parsed.subtasks:
        lines.append("**Subtasks:**")
        for st in parsed.subtasks[:8]:
            lines.append(f"- {st.index}. {st.title}")

    text = "\n".join(lines)
    return _truncate(text, _MAX_PLAN_SNIPPET)


def _extract_section(text: str, section_name: str) -> str | None:
    pattern = re.compile(
        rf"^##\s+{re.escape(section_name)}\s*$",
        re.IGNORECASE | re.MULTILINE,
    )
    match = pattern.search(text)
    if not match:
        return None
    start = match.end()
    next_hdr = re.search(r"^##\s+", text[start:], re.MULTILINE)
    end = start + next_hdr.start() if next_hdr else len(text)
    body = text[start:end].strip()
    return body or None


def _bullets(body: str) -> list[str]:
    items: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if line.startswith(("-", "*")):
            items.append(line.lstrip("-* ").strip())
    return items


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 40] + f"\n… (truncated {len(text) - n} chars)"
