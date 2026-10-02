"""Intake — forge erzeugt Arbeit (Roadmap A2 bis A4, Loop 2).

Agents schlagen neue Work-Items im ``---FORGE-WORKITEMS-...---``-Block vor
(Epic-Kinder, Funde außerhalb des Auftrags, Review-Folgeaufgaben); dazu kommen
deterministische Quellen (CI rot auf ``main``, Schedules). Dieses Modul
entscheidet, **ob und wie** daraus Items werden:

* :func:`parse_workitems` — YAML-Block → :class:`ProposedItem` (best-effort,
  fail-open: kaputter Block = keine Items; forge erfindet nie welche).
* :func:`fingerprint` — deterministischer Hash pro Fund → Dedupe.
* :func:`render_body` — Body mit ``Depends-On:``/``Touches:``/Fingerprint-Zeilen
  (vom Conductor wieder geparst, im Tracker vom Menschen editierbar).
* :func:`start_stage` — ``forge:proposed`` (Mensch gibt frei) vs. direkt
  ``forge:requirements``/``forge:epic``.
* :func:`create_items` — der Effekt, über injizierten ``WorkTracker`` + Emitter;
  hält Capability, Tages-/Epic-Obergrenzen und Dedupe ein.

Mantra 3: der Runner liefert nur den rohen Block; angelegt wird hier, in der
Fabrik-Schicht, nie in der Loop.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import yaml
from forge_core.spec import IntakeConfig
from forge_core.tracking import NewWorkItem, WorkItemKind

from forge_cli.dependencies import find_cycle

_KINDS: frozenset[str] = frozenset({"epic", "feature", "story", "bug", "task", "spec"})
_MAX_TITLE = 200
_MAX_BODY = 20_000

PROPOSED_LABEL = "forge:proposed"
GENERATED_LABEL = "forge:generated"


@dataclass(frozen=True)
class ProposedItem:
    local_id: str
    kind: WorkItemKind
    title: str
    body: str = ""
    depends_on: tuple[str, ...] = ()
    touches: tuple[str, ...] = ()


def parse_workitems(block: str | None) -> list[ProposedItem]:
    """YAML-Liste aus dem Agent-Block → Items. Unbrauchbare Einträge fallen raus."""
    if not block:
        return []
    text = block.strip()
    # Agents umschließen YAML gern mit ```-Zäunen.
    text = re.sub(r"^```[a-zA-Z]*\s*\n|\n```\s*$", "", text)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return []
    if isinstance(data, dict):
        data = data.get("items") or [data]
    if not isinstance(data, list):
        return []
    out: list[ProposedItem] = []
    seen: set[str] = set()
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            continue
        title = " ".join(str(raw.get("title") or "").split())[:_MAX_TITLE]
        if not title:
            continue
        kind = str(raw.get("kind") or "task").strip().lower()
        if kind not in _KINDS:
            kind = "task"
        local_id = str(raw.get("id") or f"item{i + 1}").strip()
        if local_id in seen:
            continue
        seen.add(local_id)
        out.append(
            ProposedItem(
                local_id=local_id,
                kind=kind,  # type: ignore[arg-type]
                title=title,
                body=str(raw.get("body") or "").strip()[:_MAX_BODY],
                depends_on=tuple(str(d).strip() for d in _as_list(raw.get("depends_on"))),
                touches=tuple(str(g).strip() for g in _as_list(raw.get("touches")) if str(g).strip()),
            )
        )
    return out


def _as_list(value) -> list:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [v for v in re.split(r"[,\s]+", str(value)) if v]


def fingerprint(source: str, title: str, scope: str = "") -> str:
    """``sha256:<hex>`` über Quelle + Scope + normalisierten Titel."""
    norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
    return "sha256:" + hashlib.sha256(f"{source}|{scope}|{norm}".encode()).hexdigest()


FINGERPRINT_PREFIX = "forge-fingerprint: "


def render_body(
    item: ProposedItem,
    *,
    depends_on: list[int],
    fp: str,
    source: str,
    origin_issue: int | None = None,
    extra_lines: tuple[str, ...] = (),
) -> str:
    lines = [item.body.strip()] if item.body.strip() else []
    meta: list[str] = list(extra_lines)
    if origin_issue is not None:
        meta.append(f"Found while working on #{origin_issue}.")
    if depends_on:
        meta.append("Depends-On: " + ", ".join(f"#{n}" for n in depends_on))
    if item.touches:
        meta.append("Touches: " + ", ".join(item.touches))
    meta.append(f"_Created by forge ({source})._")
    meta.append(f"{FINGERPRINT_PREFIX}{fp}")
    return "\n\n".join([*lines, "\n".join(meta)])


def start_stage(
    kind: str, *, source: str, cfg: IntakeConfig, parent_approved: bool = False
) -> str:
    """Start-Stage eines erzeugten Items.

    Freigegeben (direkt bearbeitbar) ist ein Item, wenn es Kind eines vom
    Menschen freigegebenen Epics ist oder sein Typ in ``auto_accept`` steht.
    Sonst wartet es in ``forge:proposed`` — eine Stage, die der Conductor nie
    selbst verlässt (Roadmap E7)."""
    approved = parent_approved or kind in cfg.auto_accept
    if not approved:
        return PROPOSED_LABEL
    return "forge:epic" if kind == "epic" else "forge:requirements"


def creation_order(items: list[ProposedItem]) -> list[ProposedItem]:
    """Topologische Reihenfolge (Abhängigkeiten zuerst), damit ``Depends-On``
    auf echte Nummern zeigen kann. Unbekannte Referenzen und Zyklus-Kanten
    werden verworfen (deterministisch, nie still Items verlieren)."""
    ids = {i.local_id for i in items}
    graph = {i.local_id: [d for d in i.depends_on if d in ids and d != i.local_id] for i in items}
    while (cycle := find_cycle(graph)) is not None:
        for node in cycle:
            graph[node] = [d for d in graph[node] if d not in cycle]
    order: list[str] = []
    placed: set[str] = set()
    by_id = {i.local_id: i for i in items}
    remaining = [i.local_id for i in items]
    while remaining:
        progressed = False
        for lid in list(remaining):
            if all(d in placed for d in graph[lid]):
                order.append(lid)
                placed.add(lid)
                remaining.remove(lid)
                progressed = True
        if not progressed:  # pragma: no cover — Zyklen sind oben aufgelöst
            order.extend(remaining)
            break
    return [
        ProposedItem(
            local_id=by_id[lid].local_id, kind=by_id[lid].kind, title=by_id[lid].title,
            body=by_id[lid].body, depends_on=tuple(graph[lid]), touches=by_id[lid].touches,
        )
        for lid in order
    ]


@dataclass
class IntakeResult:
    created: list[int] = field(default_factory=list)
    deduplicated: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    """Gründe für nicht angelegte Items (Capability, Obergrenze, Fehler)."""


def created_in_last_day(events: list, now: datetime) -> int:
    since = now - timedelta(days=1)
    return sum(1 for e in events if e.ts >= since)


def create_items(
    items: list[ProposedItem],
    *,
    tracker,
    emit: Callable[[dict], None],
    cfg: IntakeConfig,
    allowed: bool,
    source: str,
    created_today: int,
    parent: int | None = None,
    parent_approved: bool = False,
    origin_issue: int | None = None,
    scope: str = "",
    provider: str | None = None,
    extra_lines: tuple[str, ...] = (),
) -> IntakeResult:
    """Legt ``items`` beim Tracker an — mit allen Leitplanken (A3).

    ``emit`` bekommt pro angelegtem Item die ``WorkItemCreated``-Payload als
    dict (der Caller baut das Event); so bleibt die Funktion ohne Store testbar.
    """
    from forge_adapters.base import TrackerError

    result = IntakeResult()
    if not items:
        return result
    if not allowed:
        result.skipped = [f"{i.title}: capability create_work_items disabled" for i in items]
        return result
    budget = min(cfg.max_items_per_epic, max(0, cfg.max_items_per_day - created_today))
    numbers: dict[str, int] = {}
    for item in creation_order(items):
        fp = fingerprint(source, item.title, scope)
        try:
            existing = tracker.search_items(fp)
        except TrackerError:
            existing = []
        if existing:
            numbers[item.local_id] = existing[0].number
            result.deduplicated.append(existing[0].number)
            continue
        if budget <= 0:
            result.skipped.append(f"{item.title}: intake limit reached")
            continue
        deps = [numbers[d] for d in item.depends_on if d in numbers]
        stage = start_stage(item.kind, source=source, cfg=cfg, parent_approved=parent_approved)
        body = render_body(item, depends_on=deps, fp=fp, source=source,
                           origin_issue=origin_issue, extra_lines=extra_lines)
        try:
            created = tracker.create_item(
                NewWorkItem(
                    kind=item.kind,
                    title=item.title,
                    body=body,
                    labels=[stage, GENERATED_LABEL],
                    parent=parent,
                )
            )
        except TrackerError as exc:
            result.skipped.append(f"{item.title}: {exc}")
            continue
        budget -= 1
        numbers[item.local_id] = created.number
        result.created.append(created.number)
        emit(
            {
                "number": created.number,
                "kind": item.kind,
                "title": item.title,
                "source": source,
                "stage": stage,
                "parent": parent,
                "depends_on": deps,
                "expected_files": list(item.touches),
                "fingerprint": fp,
                "origin_issue": origin_issue,
                "provider": provider,
            }
        )
    return result


__all__ = [
    "FINGERPRINT_PREFIX",
    "GENERATED_LABEL",
    "PROPOSED_LABEL",
    "IntakeResult",
    "ProposedItem",
    "create_items",
    "created_in_last_day",
    "creation_order",
    "fingerprint",
    "parse_workitems",
    "render_body",
    "start_stage",
]
