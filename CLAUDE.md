# CLAUDE.md

> Architektur- und Konventions-Notizen für Pair-Programming-Sessions auf forge selbst. Lies das, bevor du Code änderst — das Dokument lebt zusammen mit dem Repo, im Gegensatz zur statischen Spec.

## Einordnung

forge ist die **messbare, replay-fähige Auto-PR-Maschine** aus `docs/forge-spec-v0.5.md` (Diff-Doku gegenüber v0.4/v0.3/v0.2, die als historische Snapshots im Repo bleiben). Die Spec ist Vertrag: wenn dein Vorschlag einem der drei Mantras widerspricht, ist es kein Bug, sondern eine Designentscheidung.

Die drei Mantras:

- Nur was messbar ist, darf die Maschine optimieren — und nicht alles Wertvolle ist messbar.
- Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
- Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist Sicherheit.

## Was forge in v1 NICHT tut

Diese Punkte sind **kategorisch ausgeschlossen** — nicht aus Vorsicht, sondern weil zuerst Daten gesammelt werden müssen, bevor sie sich rechtfertigen:

- Auto-Merge (`capabilities.merge_pr`, `push_to_main`, `push_force` sind als `Literal[False]` typisiert, nicht runtime-konfigurierbar)
- Population-Based Search (kommt mit v2, nach 100+ Runs)
- Bandit / Bayesian Optimization (kommt mit v3, nach 300+ Runs)
- Self-Improvement: forge ändert ihre eigene Konfiguration NIE (Prinzip 3, Spec Teil 7.4)

## Architektur — vier Packages

```
forge-core ──── Schema, Store, CAS, Spec, Replay (keine internen Deps)
     │
     ├── forge-execute ──── Loop 1: Runner + Strategies + Mutators + Evaluators
     │       │
     │       └── forge-cli ──── Endbenutzer-CLI
     │              │
     └── forge-adapters ──── GitHub: PR + Webhook + Action-Templates
            │
            └── forge-cli (transitiv)
```

**Boundaries sind hart**:
- `forge-core` darf keine Imports aus `forge-execute`/`forge-cli`/`forge-adapters` haben
- `forge-execute` darf keine Imports aus `forge-cli`/`forge-adapters` haben
- `forge-cli` und `forge-adapters` dürfen sich gegenseitig referenzieren

Wenn du eine Datei verschiebst, prüfe Import-Direction.

## Build / Test / Lint Commands

```bash
# Setup nach Clone
uv sync --all-packages --extra dev

# Volltest
uv run pytest                            # 163 Tests, ~40s
uv run pytest packages/forge-core        # nur ein Package
uv run pytest -v --tb=short              # verbose mit kurzem Traceback

# Lint
uv run ruff check packages/
uv run ruff check --fix packages/        # Auto-Fix

# CLI lokal
uv run forge --help
uv run forge doctor --spec examples/pinta/.forge/project.yaml
```

**Vor Commit**: `uv run pytest && uv run ruff check packages/` müssen beide grün sein.

## Code-Konventionen

### Pydantic v2

- Alle Models sind `model_config = ConfigDict(extra="forbid")` — fängt Tippfehler ab
- `Event` ist `frozen=True` — Events sind immutable nach Konstruktion
- Sub-Modelle für Enums verwenden `Literal[...]` (in Specs) oder `StrEnum` (in Code)

### Identitäten und Hashes

- Event-IDs und Run-IDs sind ULIDs (sortable nach Zeit, lexikographisch)
- Artefakt-Hashes haben das Format `sha256:<64-hex>` — der Prefix ist Pflicht und wird validiert
- Project-Fingerprint hashed `{lang, framework, file_count}` — siehe `forge_cli.runtime.compute_project_fingerprint`

### Zeit

- Alle `ts`-Felder sind `datetime` mit `tzinfo=UTC` — `Event` lehnt naive oder nicht-UTC ab
- DuckDB gibt TIMESTAMPTZ in Local-Time zurück; `_row_to_event` konvertiert zurück nach UTC

### Subprocess

- Auf Windows brauchst du `creationflags=CREATE_NEW_PROCESS_GROUP`, um Process-Trees zu killen — siehe `evaluators/command.py::_kill_tree`
- Niemals `shell=True` ohne Bewusstsein, dass die Inputs trusted sein müssen — Spec-Commands sind das, User-Issue-Bodies nicht
- Encoding: `text=True, encoding="utf-8", errors="replace"` ist Standard

### Tests

- Es gibt **kein** `tests/__init__.py` in den Packages — pytest-Discovery braucht die rootdir-Mode-Variante, sonst kollidieren gleichnamige Test-Module zwischen Packages
- Fixtures in `conftest.py` global, Helper-Functions privat in der Test-Datei
- Echte Subprozesse (git, pytest) sind ok in Tests, solange sie schnell sind — die forge-execute-Tests laufen in <30s
- Hypothesis ist installiert, aber nur für Property-Tests von gates/scoring nutzen

## Bekannte Stolperfallen

### DuckDB Performance

Single-row INSERT via Python-Binding kostet ~12 ms each, auch in-Memory. Das ist ein bekanntes Issue von DuckDB; nicht versuchen, mit Indexes oder PRAGMA zu fighten. Für Bulk → `executemany`. Für Tests, die Zeit messen, statt 1000 Events lieber 10 nehmen.

### Conductor / Heartbeat ist Loop 2 — über der Loop, nie darin

Die Fabrik-Orchestrierung (`board-loop --watch`) lebt in `forge-cli`
(`board_loop.py::_run_watch` + `heartbeat.py` + `schedule.py` + `stages.py` +
`dependencies.py` + `conductor.py`), **nie** in `forge-execute`. Der
Conductor-Kern ist **rein + testbar**: `stages.advance` (State-Machine),
`dependencies` (Graph/Zyklen), `conductor.plan_tick` (Tick-Differenz) und
`conductor.derive_signals` (Plan/PR/Merge rein aus dem Event-Strom, korreliert
über `RunStarted.issue_number`). `run_conductor_tick` effektiert über injizierte
Callables (set_stage/dispatch) — die GitHub-Seite (`list_stage_items`/
`set_issue_stage_label` im board-Adapter) ist die dünne, ersetzbare Außenschicht.
Verdrahtet als `board-loop --watch --conductor` (`_run_conductor_watch`);
board-watch und conductor-watch teilen `_heartbeat_session`. Stage-Labels
(`forge:<stage>`) fallen mit den `on_issue_label`-Trigger-Keys zusammen — ein
Label, kein zweiter Konfig-Ort. **Noch offen:** Live-Verifikation der
gh-Kommandos gegen ein echtes Board (bisher nur Stub-getestet). Mantra 3: der Heartbeat taktet das Dispatchen von Runs
(`execute_run`), greift aber nie in Runner/Scoring/Gates ein. Die
Heartbeat-Engine (`run_heartbeat`) ist mit injizierten Deps (sleep/should_stop/
emit) ohne echtes `time.sleep` testbar — Tests nutzen `max_ticks`. Jeder Tick
ist ein `ConductorTickCompleted`-Event unter einer **Session-ULID** als
`run_id` (Fabrik-Events stehen über den Run-Events, kein Envelope-Umbau). Der
Cron-Matcher (`schedule.py`) ist dependency-frei (kein croniter). Design +
Phasenplan: `docs/conductor-design.md`. Beim Erweitern des board-loop: den
Dispatch-Pfad (`_dispatch_issues`) teilen Single-Pass und Watch-Tick — nicht
duplizieren.

### Fabrik-Metriken leben in Views, nicht in der Loop

Die „Software-Factory"-Sicht (Aggregation ÜBER Runs hinweg) ist bewusst **read-only** und liegt komplett in DuckDB-Views (`store.py`, `_VIEW_FACTORY_KPIS` + `_VIEW_FACTORY_THROUGHPUT`), gerendert von `forge analyze` (`analyze.py`). Sie berührt die Loop nie (Mantra 3) — reine Auswertung des Event-Stroms. KPIs: Durchsatz, Merge-Rate, Keep-Rate (keep/discard-Generationen), Kosten pro gemergtem PR, Lead-Time (`PRMerged.time_to_merge_s`). Wenn du eine neue Fabrik-Metrik brauchst: neuen View dazu, in `_VIEWS` registrieren, Sektion in `analyze.py` ergänzen — **keine** neue Event-Logik, **kein** Loop-Eingriff. Das ist die Datengrundlage, die v2 (Population) laut Spec voraussetzt (≥100 Runs + Plateau).

### Scoring bei rot→grün

Wenn die Baseline-Gates nicht passen (Tests rot) und nach der Mutation passen, ist das **immer** ein Improvement, unabhängig vom Composite-Delta. Siehe `scoring.keep_or_discard(baseline_gates_passed=False)`. Vergessen → der `legacy_test_revival`-Pfad bleibt stuck.

### Judge ist fail-closed, Triage ist fail-open

Beide sind opt-in LLM-Pre/Sub-Phasen, aber mit **entgegengesetzter** Fehlerpolitik — nicht verwechseln. Die Triage lässt im Zweifel *durch* (`decision="relevant"`), weil ein zu Unrecht geschlossenes Issue teurer ist als ein unnötiger Run. Der Judge (`evaluators/judge.py`) lässt im Zweifel *fallen* (`score=0.0`/`fail`), weil ein unverifizierter Diff nie gemerged werden darf. Wenn du am Judge schraubst und einen Default einbaust, der bei Fehler „durchwinkt", brichst du die Sicherheitseigenschaft.

Der Judge trägt sein Ergebnis als `llm_judge_score`-Messwert in den Eval-Output und bindet über ein gewöhnliches Gate. Die Decide-Logik (`scoring.py`/`gates.py`) bleibt **unangetastet** — der Judge füttert nur ein, er entscheidet nicht (Mantra 3). Vor der Implementierung fehlt der Messwert → Gate rot → nach Judge-`pass` grün → `gate_revival`. Das ist derselbe rot→grün-Pfad wie oben, nur dass die „Röte" vom Judge statt von pytest kommt.

### Forbidden Zones überschneiden Surfaces

Die Spec-Validierung verbietet exakte String-Overlaps (`forbidden: ["src/foo/"]` und `surface.paths: ["src/foo/"]`). Glob-Overlap ist gewollt erlaubt: `surface: ["src/**"]` mit `forbidden: ["src/secrets.py"]` ist OK — Forbidden ist die feinere Auflösung.

### Pathspec API

Nutze `GitIgnoreSpec.from_lines(...)` (modern API), nicht `PathSpec.from_lines(GitWildMatchPattern, ...)` (deprecated). `pathspec >= 0.12`.

### CRLF-Warnings

Windows-Default. Ignorieren. Wenn du sie wirklich loswerden willst: `git config core.autocrlf false`. Inhalte bleiben identisch.

### Auto-Merge ist Spec-Grauzone, nicht Spec-Bruch

`forge board-loop --auto-merge` und `forge run --auto-merge` rufen
`gh pr merge --auto --squash --delete-branch` auf. Das **queued** den
Merge bei GitHub — der eigentliche Merge passiert server-seitig, von
GitHubs Bots, asynchron, sobald alle required Checks grün sind. forge
selbst führt **keinen** synchronen `merge`-Subprozess aus.

Das ist die erlaubte Lesart der `merge_pr`-Capability (typed
`Literal[False]`): die Capability verbietet, dass forge selbst mergt
(forge ruft nicht `gh pr merge <N>` ohne `--auto` auf). GitHub mergt
auf Operator-Anfrage hin, nicht auf forge-Anfrage. Gleiche Logik wie
`release.on_main_green: auto_tag` — Tagging ist erlaubt (kein Code-
Change), und Auto-Merge ist erlaubt-via-GitHub-Feature, nicht erlaubt-
via-forge-Subprozess.

Wenn du das nicht willst: einfach `--auto-merge` weglassen. PR wird
geöffnet, Operator mergt manuell. Default-Verhalten bleibt **kein**
Auto-Merge — der Flag ist explizit opt-in pro Aufruf.

Source: `forge_adapters.github.pr.queue_auto_merge` Docstring.

## Event-Schema — Achtung, irreversibel

`payload_schema_version` ist **pro Kind** versioniert (Spec Teil 4.1). Wenn du ein bestehendes Sub-Schema änderst:

- Additive Änderungen (neues optionales Feld) → `1.0` → `1.1`, alte Events lesen weiterhin
- Breaking Änderungen → eigentlich nicht erlaubt in v1, weil historische Daten nicht migriert werden
- Neuer EventKind → neue Datei in `events/kinds/`, `register_payload(...)` aufrufen

Vor jeder Schema-Änderung: `len(EventKind) == 18` und `len(_PAYLOAD_REGISTRY) == 18` testen (v0.4 = v0.3-17 + `ISSUE_TRIAGED`).

`PlanProposed` steht auf Schema **1.1** (additiv: `subtasks: list[PlanSubtask]` + `agents_used: list[str]`). Alte 1.0-Events lesen weiter, weil beide Felder Defaults haben — siehe `test_plan_proposed_schema_is_v1_1_with_additive_fields`.

## CodingAgent ist Plug-in, nicht Fundament

`ClaudeCodeCLIAgent` ist eine konkrete Implementierung. Das `CodingAgent`-Protocol erlaubt morgen einen `CodexCLIAgent`, `OpenCodeAgent` oder `DirectAnthropicAPIAgent`. Wenn du im Runner gegen `claude` direkt programmierst statt gegen das Protocol, brichst du das.

### Multi-Agent ist Plug-in-intern, nicht Runner-Sache

Das Subagent-Team (architect → developer → tester → reviewer) wird **vom Master-`claude` orchestriert**, nicht vom Runner. Der Runner ruft ein einziges `propose()` auf; die Rollen-Choreografie lebt komplett im `ClaudeCodeCLIAgent`. Würde der Runner die Schritte selbst takten, müsste die Loop die Agent-Rollen kennen — das bricht Mantra 3 und das Plug-in-Prinzip.

Der **reviewer** ist das opt-in 4. Arbeitspferd (`KNOWN_AGENTS`, aber **nicht** in `DEFAULT_AGENTS`): read-only (`Read, Glob, Grep, Bash`), läuft als LETZTER Schritt nach der Tester-Verifikation und liest den kumulativen Diff kritisch gegen (Korrektheit, Surfaces/Forbidden, Security, Test-Qualität). Er editiert nichts — BLOCKING-Findings gehen zurück an den Developer (zwei Runden max), dann re-verifiziert der Tester. Damit bleibt Mantra 3 intakt: der reviewer urteilt, er entscheidet nicht und schreibt keinen Code. **Abgrenzung zum Judge:** Der Judge (`evaluators/judge.py`, `review()`) ist die *fail-closed, gescorte* Loop-Phase, die `llm_judge_score` in ein Gate füttert und keep/discard mitentscheidet — read-only, **außerhalb** der Orchestrierung. Der reviewer ist *in-Orchestrierung*, qualitativ, und verbessert den Diff *bevor* er den Worktree verlässt. Sie sind komplementär, nicht redundant.

Welche Rollen mitwirken, steuert die `agents:[...]`-Roster aus der Trigger-Config (`spec.triggers.on_issue_label[label].agents`). Der board-loop löst das Roster pro Issue-Label auf (`_roster_for_issue`), `forge run` nimmt `--agents a,b,c` (oder `--multi-agent` als Shortcut fürs Default-Roster). Das Roster fließt in:
- `ClaudeCodeCLIAgent(agents=...)` → `build_orchestrator_prompt(roster)` baut den `--append-system-prompt` aus genau den aktiven Rollen; ohne `architect` entfallen Plan-Schritt + `---FORGE-PLAN-...---`-Marker, ohne `tester` die Verifikations-Schritte, ohne `reviewer` der Review-Schritt + die BLOCKING-Findings-Schleife. `_install_subagents(wt, agents=roster)` kopiert nur die Roster-`.md`s in den Worktree.
- `RunConfig.agents` ist das **konfigurierte** Roster. `PlanProposed.agents_used` trägt dagegen die **tatsächlich gerufenen** Rollen: der Master meldet sie im `---FORGE-AGENTS-BEGIN/END---`-Block (`extract_agents_from_master_output`), der Runner setzt `agents_used = agents_invoked or list(config.agents)` — Selbstauskunft gewinnt, Config ist nur Fallback. Gleicher Trust-Model wie die Plan-Checkboxen: best-effort, aber misst Wirklichkeit statt Absicht (Mantra 1). Kein Schema-Bump — das Feld bedeutete schon immer „mitgewirkt".

### Modell pro Rolle: via Projekt-Override

Die `.md`-Templates tragen `model: sonnet` im Frontmatter. Wer eine Rolle auf ein anderes Modell heben will (z.B. `architect` auf opus), legt ein Projekt-Override unter `<repo>/.forge/agents/<role>.md` mit eigenem Frontmatter ab — `_install_subagents` kopiert die ganze Datei (inkl. `model:`-Zeile) über den Default. Das funktioniert in BEIDEN Pfaden (`forge run` + board-loop), weil beide durch denselben Hybrid-Lookup gehen. Kein Spec-Feld nötig, kein zweiter Konfig-Ort.

### Was die Orchestrierung (noch) NICHT misst

- **Per-Subagent-Telemetrie** (Cost/Tokens/Turns pro Rolle): `--output-format json` liefert nur Master-Totals. Eine echte Aufschlüsselung bräuchte `--output-format stream-json` + Stream-Parser — ein separater, gegen echtes `claude` zu verifizierender Change, kein blinder Umbau des funktionierenden propose-Pfads.
- **„Zwei Runden max"** bei roter Verifikation/BLOCKING-Findings ist bewusst eine Prompt-Instruktion, **kein** vom Runner gezähltes Limit: die harte Ressourcen-Grenze sind Cost-Caps + `max_turns` (`_check_run_cost_cap`). Würde der Runner die Retry-Runden zählen, müsste er die Orchestrierungs-Schritte kennen → Mantra-3-Bruch.

Ein einsamer `["developer"]` braucht keine Orchestrierung (`roster_needs_orchestration`) — das ist der klassische Single-Agent-Run (kein Task-Tool, kein Plan). Das alte `multi_agent: bool` bleibt rückwärtskompatibel (`True` = Default-Roster, `False` = `["developer"]`).

## Sicherheit — vier Schichten

1. **Forbidden Zones** (Pfad-Ebene) — `Capabilities.check_edit/read`
2. **Capabilities** (Aktions-Ebene) — `Capabilities.check_action/run/egress`
3. **Cost-Caps** (Ressourcen-Ebene) — `SequentialRunner._check_run_cost_cap`
4. **Subprocess-Isolation** (Prozess-Ebene) — Worktree pro Run, separate venv kommt in M2

`merge_pr` / `push_to_main` / `push_force` sind dreifach gesichert: in der Spec via `Literal[False]`, in `Capabilities.check_action` als hartkodiertes Deny, und im PR-Body steht der Hinweis explizit.

## Wenn du nicht sicher bist

- **Eine neue Mutation einführen?** Trag sie in `forge_core.events.kinds.mutation.py` als neuen `MutatorKind` ein, schreibe ein eigenes Modul in `forge_execute/mutators/`, registriere im Runner.
- **Eine neue Eval-Suite?** Erweitere `EvalSuiteConfig.parses` um den neuen Modus, schreibe einen Parser in `evaluators/command.py`, hänge ihn an `_parse()` an.
- **Eine neue Capability?** Erweitere `CapabilitiesConfig`, dann `Capabilities`-Klasse, dann Tests doppelt schreiben — Capabilities sind die wichtigste Verteidigungslinie.

Wenn dein Change ein bestehendes Modul „etwas größer" macht statt klar zuordenbar zu sein: stop, denk nach, frag den Operator. Das ist meistens ein Anzeichen, dass die Schichtung verletzt würde.
