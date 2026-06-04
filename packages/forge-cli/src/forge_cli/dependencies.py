"""Dependency-Graph für Work-Items (Conductor / Loop 2).

Koordination ohne Reihenfolge ist nur Parallelität. Work-Items deklarieren
Abhängigkeiten über eine Zeile im Issue-Body:

    Depends-On: #12, #15

Dieses Modul ist **rein**: Parser, Graph-Aufbau, Zyklus-Erkennung und die
Frage „welche Items sind dependency-frei?". Deterministisch und damit
replay-fähig — bewusst KEINE LLM-Inferenz von Abhängigkeiten (v1).
"""

from __future__ import annotations

import re

# Erkennt `Depends-On: #12, #15` / `depends on: #3` / `Blocked-By #7` am
# Zeilenanfang (case-insensitive). Nur Operator-Config-Text, kein User-Input
# im Sicherheitssinn — trotzdem eng gefasst.
_DEPENDS_LINE = re.compile(
    r"(?im)^\s*(?:depends[\s_-]?on|blocked[\s_-]?by)\s*:?\s*(.+)$"
)
_ISSUE_REF = re.compile(r"#(\d+)")


def parse_depends_on(body: str | None) -> list[int]:
    """Extrahiert die Issue-Nummern, von denen ein Work-Item abhängt.

    Sammelt alle ``#N``-Referenzen aus allen ``Depends-On:``/``Blocked-By:``
    Zeilen im Body. Dedupliziert, Reihenfolge = erstes Auftreten. Leerer/None-
    Body → leere Liste.
    """
    if not body:
        return []
    out: list[int] = []
    seen: set[int] = set()
    for line in _DEPENDS_LINE.findall(body):
        for ref in _ISSUE_REF.findall(line):
            n = int(ref)
            if n not in seen:
                seen.add(n)
                out.append(n)
    return out


def find_cycle(graph: dict[int, list[int]]) -> list[int] | None:
    """Findet einen Zyklus im Dependency-Graph (gerichtet: item → deps).

    Liefert die Knoten eines gefundenen Zyklus (in Reihenfolge), oder ``None``
    wenn der Graph azyklisch ist. DFS mit Drei-Farben-Markierung; Kanten zu
    unbekannten Knoten (Dependency außerhalb des bekannten Sets) werden
    ignoriert — die werden separat als „offen" behandelt, nicht als Zyklus.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    color: dict[int, int] = {n: WHITE for n in graph}
    stack: list[int] = []

    def dfs(node: int) -> list[int] | None:
        color[node] = GREY
        stack.append(node)
        for dep in graph.get(node, []):
            if dep not in color:
                continue  # unbekannte Dependency → kein Zyklus, separat behandelt
            if color[dep] == GREY:
                # Zyklus: vom ersten Auftreten von dep bis jetzt.
                idx = stack.index(dep)
                return [*stack[idx:], dep]
            if color[dep] == WHITE:
                found = dfs(dep)
                if found is not None:
                    return found
        color[node] = BLACK
        stack.pop()
        return None

    for node in graph:
        if color[node] == WHITE:
            found = dfs(node)
            if found is not None:
                return found
    return None


def unmet_dependencies(
    number: int,
    depends_on: list[int],
    *,
    done: set[int],
    known: set[int],
) -> list[int]:
    """Die Dependencies eines Items, die (noch) nicht ``done`` sind.

    Eine Dependency gilt als unerfüllt, wenn sie nicht in ``done`` steht —
    inklusive Dependencies, die gar nicht im ``known``-Set sind (offenes
    Issue, das der Conductor (noch) nicht kennt → konservativ blockieren).
    Reihenfolge = Eingabe.
    """
    return [d for d in depends_on if d not in done]
