# Contributing to forge

Diese Anleitung richtet sich an **Entwickler, die forge selbst weiterentwickeln**.
Wer forge nur *benutzen* will, ist im [`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)
richtig.

Drei Dokumente gehören zusammen:

- **[`CLAUDE.md`](CLAUDE.md)** — das zentrale Architektur- & Konventions-Dokument.
  Die dichte „Stolperfallen"-Sammlung dort ist Pflichtlektüre, bevor du Code
  änderst. Diese Datei hier ist der schlanke Einstieg und der Prozess; die Details
  stehen in CLAUDE.md.
- **[`docs/forge-spec-v0.6.md`](docs/forge-spec-v0.6.md)** — der Vertrag. Diff-Doku
  über v0.5/v0.4/…; ältere Versionen bleiben als Snapshots.
- **[`docs/conductor-design.md`](docs/conductor-design.md)** — Design von Loop 2.

---

## Die drei Mantras sind Design-Vertrag

Wenn dein Vorschlag einem der drei widerspricht, ist es **kein Bug, sondern eine
Designentscheidung** — diskutiere es zuerst, statt es zu „fixen":

1. Nur was messbar ist, darf die Maschine optimieren — und nicht alles Wertvolle
   ist messbar.
2. Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
3. Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist Sicherheit.

Praktische Konsequenz von Mantra 3: die **Loop** (`forge-execute`, Loop 1) und der
**Conductor** (`forge-cli`, Loop 2) takten Arbeit an, greifen aber nie in
Runner/Scoring/Gates ein. Auswertung (`analyze`, Memory) ist **read-only** über den
Event-Strom.

---

## Architektur in einem Bild

```
forge-core ──── Schema, Store (DuckDB), CAS, Spec, Replay   (keine internen Deps)
   │
   ├── forge-execute ──── Loop 1: Runner + Strategies + Mutators + Evaluators + Judge
   │       │
   │       └── forge-cli ──── 9 Befehle + Loop 2 (Conductor/Heartbeat/board-loop)
   │              │
   └── forge-adapters ──── GitHub: PR + Webhook + Project-Board + Action-Templates
          │
          └── forge-cli (transitiv)
```

**Boundaries sind hart** (PR wird abgelehnt, wenn verletzt):

- `forge-core` darf **nichts** aus `forge-execute`/`forge-cli`/`forge-adapters`
  importieren.
- `forge-execute` darf nichts aus `forge-cli`/`forge-adapters` importieren.
- `forge-cli` und `forge-adapters` dürfen sich gegenseitig referenzieren.

Wenn du eine Datei verschiebst, prüfe die Import-Richtung.

---

## Dev-Setup

```bash
git clone <forge-repo>
cd forge
uv sync --all-packages --extra dev
```

### Build / Test / Lint

```bash
uv run pytest                            # volle Suite (~571 Tests)
uv run pytest packages/forge-core        # nur ein Package
uv run pytest -v --tb=short              # verbose, kurzer Traceback

uv run ruff check packages/              # Lint
uv run ruff check --fix packages/        # Auto-Fix

uv run forge --help                      # CLI lokal
```

**Vor jedem Commit müssen beide grün sein:**

```bash
uv run pytest && uv run ruff check packages/
```

---

## Branch- & Commit-Konventionen

- **Nie direkt auf `main` committen.** Branch zuerst (`feat/…`, `fix/…`,
  `docs/…`, `test/…`). Das Repo arbeitet PR-basiert.
- **Conventional Commits** (`feat(scope): …`, `fix(scope): …`, `docs: …`). Der
  `release`-Block der Spec setzt das voraus.
- Vor dem Push gegen den aktuellen `origin/main` rebasen — `main` bewegt sich
  schnell (mehrere PRs/Tag).
- Wenn dein Commit ein Modul „etwas größer" macht statt klar zuordenbar zu sein:
  stop, nachdenken, fragen. Das ist meist ein Zeichen, dass die Schichtung
  verletzt würde.

---

## Wo gehört was hin? (Erweiterungs-Rezepte)

Die ausführlichen Rezepte stehen in [`CLAUDE.md`](CLAUDE.md) („Wenn du nicht
sicher bist"). Kurzfassung:

| Du willst … | … dann |
|---|---|
| eine neue **Mutation** | `MutatorKind` in `forge_core.events.kinds.mutation`, Modul in `forge_execute/mutators/`, im Runner registrieren |
| eine neue **Eval-Suite** | `EvalSuiteConfig.parses`-Modus erweitern, Parser in `evaluators/command.py`, an `_parse()` hängen |
| eine neue **Capability** | `CapabilitiesConfig` → `Capabilities`-Klasse → Tests **doppelt** (Capabilities sind die wichtigste Verteidigungslinie) |
| einen neuen **CodingAgent** | das `CodingAgent`-Protocol implementieren (z.B. `CodexCLIAgent`) — **nicht** gegen `claude` direkt im Runner programmieren |
| eine neue **Conductor-Stage** | Eintrag in `IN_PLACE_WORK_STAGES` + Advance-Signal in `StageSignals`/`advance` + Dispatch-Zweig (`forge-cli`, nie `forge-execute`) |

### Event-Schema ändern — Achtung, irreversibel

`payload_schema_version` ist **pro Kind** versioniert. Additive Änderungen
(neues optionales Feld mit Default) sind erlaubt (`1.0`→`1.1`, alte Events lesen
weiter). Breaking Changes sind in v1 nicht erlaubt — historische Daten werden
nicht migriert. Neuer EventKind = neue Datei in `events/kinds/` +
`register_payload(...)`. Aktuell gilt die Größen-Invariante
`len(EventKind) == 26` (Test in `packages/forge-core/tests/test_events.py`).

---

## forge mit forge erweitern (Dogfooding)

forge kann sich selbst weiterentwickeln. Der erprobte Ablauf:

1. **Spec für das forge-Repo anlegen** (`.forge/project.yaml`), die den Agenten
   eng auf das relevante Package scoped (`surfaces: packages/forge-cli/...`) und
   alles andere `forbidden` setzt — inkl. der anderen Packages, `.forge/**`,
   `.github/**`, `docs/**`.
2. **Akzeptanztest zuerst schreiben** und committen — er ist ROT, bis das Feature
   existiert. Das ist der `gate_revival`-Anker (siehe rot→grün-Regel im
   USER_GUIDE). Lege ihn in `forbidden`, damit der Agent ihn nicht abschwächt;
   das Eval-Gate führt ihn trotzdem aus.
3. **`forge doctor`** → **`forge run --dry-run`** (validiert Pipeline für $0) →
   echter Run (`--multi-agent --model sonnet`, Cost-Cap in der Spec).
4. Ergebnis reviewen (`git show`, Tests laufen lassen), dann als regulären PR
   einbringen.

**Bekannte Stolperfalle (Windows):** Ein Eval-Command mit `uv run …` legt ein
`.venv` im Worktree an; bei einem DISCARD scheiterte `git clean -fdx` früher an
gesperrten `.pyd`-DLLs. `revert()` schließt `.venv` inzwischen aus und toleriert
Lock-Warnungen — falls du eigene Eval-Setups baust, halte regenerierbare
Artefakte aus dem Worktree-Root.

---

## Sicherheit — vier Schichten

1. **Forbidden Zones** (Pfad) — `Capabilities.check_edit/read`
2. **Capabilities** (Aktion) — `Capabilities.check_action/run/egress`
3. **Cost-Caps** (Ressource) — `SequentialRunner._check_run_cost_cap`
4. **Subprocess-Isolation** (Prozess) — ein Worktree pro Run

`merge_pr` / `push_to_main` / `push_force` sind dreifach gesichert (Spec
`Literal[False]`, hartkodiertes Deny in `Capabilities`, Hinweis im PR-Body).
Wenn du an Capabilities arbeitest: Tests **doppelt** schreiben.

---

## Was forge in v1 NICHT tut

Kategorisch ausgeschlossen (zuerst Daten sammeln): Auto-Merge durch forge selbst,
Population-Based Search (v2), Bandit/BO (v3), Self-Improvement (forge ändert ihre
eigene Config nie). Vorschläge in diese Richtung sind Roadmap, kein PR.

---

## Pull Requests

- Halte PRs klein und einer Sache gewidmet.
- `uv run pytest && uv run ruff check packages/` müssen grün sein.
- Bei Schema-/Spec-Änderungen: die Spec (`docs/forge-spec-v0.x.md`) im selben PR
  mitziehen — Code und Vertrag dürfen nicht auseinanderlaufen.
- Beschreibe im PR-Body, gegen welches Mantra/welche Spec-Stelle dein Change
  spielt, falls er einen Grenzfall berührt.
