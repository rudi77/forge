# forge

> forge ist in v1 eine messbare, replay-fähige Auto-PR-Maschine.

Daraus entsteht — wenn die Maschine zuverlässig läuft und genug Daten gesammelt sind — eine Software-Fabrik. Aber nicht umgekehrt.

## Status

Working Draft v0.2 — siehe [`docs/forge-spec-v0.2.md`](docs/forge-spec-v0.2.md).

## Packages

| Package | Verantwortung |
|---|---|
| `forge-core` | Event-Schema, Store, Spec-Loader, CAS-Blobs, Replay |
| `forge-execute` | Loop 1 — Runner, Strategies, Mutators, Evaluators, Capabilities |
| `forge-adapters` | GitHub, Slack — Integrationen ohne Logik |
| `forge-cli` | `forge run`, `forge analyze`, `forge doctor`, `forge replay` |

## Drei Sätze als Mantra

- Nur was messbar ist, darf die Maschine optimieren — und nicht alles Wertvolle ist messbar.
- Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
- Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist Sicherheit.
