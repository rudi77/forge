# forge

> **forge ist in v1 eine messbare, replay-fähige Auto-PR-Maschine.**
> Daraus entsteht — wenn die Maschine zuverlässig läuft und genug Daten gesammelt sind — eine Software-Fabrik. Aber nicht umgekehrt.

[![Tests](https://img.shields.io/badge/tests-163%20passing-green)]() [![Status](https://img.shields.io/badge/status-M1%20in%20progress-yellow)]()

---

## Was forge tut

forge nimmt einen Trigger (Issue, roter CI-Build, scheduled Optimierungslauf) entgegen, propagiert ihn durch eine Sequenz von **Propose → Mutate → Preflight → Eval → Decide**, und produziert am Ende einen Pull Request. Jeder Schritt emittiert typisierte Events. Die Events sind die einzige Wahrheit, aus der spätere Auswertung — zunächst manuell, später bandit- und BO-gesteuert — Empfehlungen ableitet.

Der Mensch ist Operator: er definiert Ziele, Constraints und Erfolgskriterien. Die Maschine erledigt die Arbeit. Der Mensch reviewt und merged. **Auto-Merge ist in v1 kategorisch ausgeschlossen.**

## Drei Sätze als Mantra

- Nur was messbar ist, darf die Maschine optimieren — und nicht alles Wertvolle ist messbar.
- Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
- Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist Sicherheit.

## Systemüberblick

forge nimmt einen Trigger entgegen, löst aus der `agents:[...]`-Config das Subagent-Roster auf und fährt pro Generation die fünf Phasen. Jede Phase emittiert typisierte Events in den Store (DuckDB + CAS); aus diesen Events speisen sich Replay und Analyse. Entsteht ein verbesserter Stand, erzeugt der GitHub-Adapter einen PR — **nie** ein Auto-Merge.

```mermaid
flowchart TD
    T1["GitHub-Issue (Label)"] --> RES
    T2["forge run --agents …"] --> RES
    T3["CI-Failure / Schedule"] --> RES

    RES["Roster auflösen<br/>spec.triggers.*.agents"] --> RUN
    RUN["SequentialRunner.run()<br/>Baseline-Eval → RUN_STARTED"] --> P1

    subgraph G["Generation — Loop 1 (forge-execute)"]
        direction TB
        P1["① Propose<br/>agent.propose()<br/>PLAN_PROPOSED · PROPOSAL_RECEIVED"]
        P2["② Mutate / Validate<br/>Capability + Syntax<br/>MUTATION_APPLIED · GUARDRAIL_VIOLATION"]
        P3["③ Preflight<br/>Surface-Guardrails<br/>PREFLIGHT_FAILED"]
        P4["④ Eval (+ opt. Judge)<br/>EVAL_STARTED · EVAL_FINISHED"]
        P5["⑤ Decide<br/>keep_or_discard<br/>DECISION_MADE · GENERATION_FINISHED"]
        P1 --> P2 --> P3 --> P4 --> P5
    end

    P5 -->|"weitere Generation"| P1
    P5 --> FIN["RUN_FINISHED"]
    FIN -->|"decision = pr_created"| PR["forge-adapters/github<br/>PR erzeugen (kein Auto-Merge)"]

    P1 -. Events .-> STORE
    P3 -. Events .-> STORE
    P5 -. Events .-> STORE
    STORE["EventStore (DuckDB)<br/>+ BlobStore (CAS) · forge-core"] --> REPLAY["forge replay · forge analyze"]
```

### Multi-Agent-Orchestrierung (Phase 1 „Propose")

Die Schritt-Choreografie lebt **im `ClaudeCodeCLIAgent`-Plug-in**, nicht im Runner (Mantra 3). Welche Arbeitspferde mitwirken, steuert das Roster aus der Trigger-Config. Der Master-`claude` orchestriert architect → developer → tester via Task-Tool; forge persistiert den Plan als `PLAN_PROPOSED`-Event mit strukturierten Subtasks.

```mermaid
flowchart TD
    CFG["Trigger-Config<br/>agents: architect · developer · tester"] --> AG
    AG["ClaudeCodeCLIAgent(agents=roster)"] --> ORC
    AG --> INST["_install_subagents(roster)<br/>nur Roster-.md → .claude/agents/"]
    ORC["build_orchestrator_prompt(roster)<br/>--append-system-prompt"] --> MASTER

    MASTER["Master „claude -p“<br/>Orchestrator · Task-Tool"]
    MASTER -->|"① planen (read-only)"| ARCH["architect<br/>Read · Glob · Grep"]
    MASTER -->|"② pro Subtask"| DEV["developer<br/>Read · Edit · Write · Bash"]
    MASTER -->|"③ verifizieren"| TEST["tester<br/>pytest · lint"]

    ARCH -->|"Plan (Markdown)"| MASTER
    MASTER -->|"FORGE-PLAN-Marker"| EXTRACT["extract_plan + parse_plan"]
    EXTRACT --> EVENT["PLAN_PROPOSED<br/>subtasks · agents_used"]
    DEV -->|"Diff im Worktree"| BACK["zurück an SequentialRunner<br/>Validate → Eval → Decide"]
```

> Ein einsames `agents: [developer]`-Roster überspringt die Orchestrierung (kein Plan, kein Task-Tool) — der klassische Single-Agent-Run.

## Status

| Schritt | Inhalt | Stand |
|---|---|---|
| 1 | Repo-Skeleton (uv-Workspace, 4 Packages) | ✅ |
| 2 | `forge-core` — Events, CAS, DuckDB, Spec, Replay | ✅ |
| 3 | `forge-execute` — Loop 1 mit allen 5 Phasen | ✅ |
| 4 | `forge-cli` + `forge-adapters/github` | ✅ |
| 5 | PINTA-Integration | ⏳ |
| v0.4 | Board-Trigger + Issue-Triage + Auto-Merge-Queue | ✅ |
| v0.5 | LLM-Judge — Verifikation gegen Akzeptanzkriterien (opt-in) | ✅ |

Detailierter Fortschritt: [`docs/progress.md`](docs/progress.md).
Aktuelle Spec: [`docs/forge-spec-v0.5.md`](docs/forge-spec-v0.5.md) (Diff-Doku; ältere Versionen bleiben als Snapshots).

## Packages

| Package | Verantwortung |
|---|---|
| `forge-core` | Event-Schema (18 Kinds), DuckDB-Store, Content-Addressed Blob-Store, `project.yaml`-Loader, Replay-API |
| `forge-execute` | Loop 1 — `SequentialRunner`, Strategies, Mutators, Evaluators, Gates+Scoring, Capabilities, Worktrees, CodingAgent-Protocol |
| `forge-adapters` | Integrationen — GitHub (PR-Erzeugung, Webhook, Action-Templates) |
| `forge-cli` | `forge run`, `forge analyze`, `forge doctor`, `forge replay` |

## Quick start

### Voraussetzungen

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), Git
- Optional: [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code), [`gh`](https://cli.github.com/) (für PR-Erzeugung)

### Installation

```bash
git clone <forge-repo>
cd forge
uv sync --all-packages --extra dev
```

### Smoke-Test

```bash
# 1. Tests laufen lassen
uv run pytest                              # 163 Tests, ~40s

# 2. CLI verfügbar?
uv run forge --help

# 3. Health-Check gegen die Beispiel-Spec
uv run forge doctor --spec examples/pinta/.forge/project.yaml
```

### Erster echter Run gegen ein eigenes Repo

```bash
cd /pfad/zu/deinem/repo
mkdir -p .forge
cp /pfad/zu/forge/examples/pinta/.forge/project.yaml .forge/
# project.yaml an dein Projekt anpassen — siehe Spec Teil 5

# Trockenlauf ohne Claude (mit Mock-Agent, schreibt aber Events)
uv run forge run --focus legacy_test_revival --dry-run --max-iterations 3

# Echter Lauf (braucht ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-...
uv run forge run --focus legacy_test_revival --max-iterations 3 --create-pr

# Reports
uv run forge analyze
uv run forge replay <run_id>
```

## Binary bauen

Ein eigenständiges `forge`-Binary (kein Python-Setup nötig zum Ausführen)
entsteht via PyInstaller:

```bash
uv run --with pyinstaller python packaging/build_binary.py
# Ergebnis: dist/forge  (Linux/macOS)  bzw.  dist/forge.exe  (Windows)
```

Die CI (`.github/workflows/ci.yml`) baut bei jedem Push die `forge.exe`
auf einem Windows-Runner und lädt sie als Artefakt hoch (`forge-windows-x64`).
Tag-Pushes (`v*`) hängen das Binary zusätzlich an ein GitHub-Release.

## Repo-Layout

```
forge/
├── packages/
│   ├── forge-core/          # Schema, Store, CAS, Spec, Replay
│   ├── forge-execute/       # Loop 1 — Runner, Strategies, Mutators, Evaluators
│   ├── forge-adapters/      # GitHub (PR + Webhooks + Action-Templates)
│   └── forge-cli/           # forge run / analyze / doctor / replay
├── packaging/               # PyInstaller-Entry + reproduzierbarer Binary-Build
├── .github/workflows/       # CI: Test + Lint + forge.exe-Build
├── examples/
│   └── pinta/               # Reference-Spec
├── docs/
│   ├── forge-spec-v0.2.md   # Vollständige Spec
│   ├── progress.md          # M1-Checkliste
│   └── todos.txt            # Original-Implementierungsplan
├── CLAUDE.md                # Architektur-Notizen für Pair-Programming
├── CHANGELOG.md
└── pyproject.toml           # uv-Workspace
```

## Lizenz

TBD — bis Phase 4 (≥100 Runs Daten) keine externen Beiträge.

## Kontakt

Issues / Diskussionen: GitHub. Architektur-Spec ist Vertrag — wenn du etwas vorschlägst, das den Mantra-Sätzen widerspricht, ist es kein Bug, sondern eine Designentscheidung.
