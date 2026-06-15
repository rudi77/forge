# CLAUDE.md

Read the following file for additional important information:
- [CLAUDE_BEHAVIORAL.md](CLAUDE_BEHAVIORAL.md)

> Architektur- und Konventions-Notizen für Pair-Programming-Sessions auf forge selbst. Lies das, bevor du Code änderst — das Dokument lebt zusammen mit dem Repo, im Gegensatz zur statischen Spec.

## Einordnung

forge ist die **messbare, replay-fähige Auto-PR-Maschine** aus `docs/forge-spec-v0.5.md` (Diff-Doku gegenüber v0.4/v0.3/v0.2, die als historische Snapshots im Repo bleiben). Die Spec ist Vertrag: wenn dein Vorschlag einem der drei Mantras widerspricht, ist es kein Bug, sondern eine Designentscheidung.

Die drei Mantras:

- Nur was messbar ist, darf die Maschine optimieren — und nicht alles Wertvolle ist messbar.
- Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
- Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist Sicherheit.

## Was forge in v1 NICHT tut

Diese Punkte sind **kategorisch ausgeschlossen** — nicht aus Vorsicht, sondern weil zuerst Daten gesammelt werden müssen, bevor sie sich rechtfertigen:

- `push_to_main` / `push_force` (als `Literal[False]` typisiert, nicht runtime-konfigurierbar)
- ~~Auto-Merge~~ — **geändert**: `capabilities.merge_pr` ist seit dem Agent-Review-Merge **opt-in** (`bool`, default `false`). forge darf einen offenen PR nach Agent-Review (verdict `approve`) + grünem CI selbst mergen (`forge review-pr`, `gh pr merge`). Siehe Abschnitt „Agent-Review-Merge". Das ist die bewusste Abkehr vom ursprünglichen kategorischen v1-Ausschluss.
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

### DuckDB Single-Writer: Threads ja, Prozesse nein

DuckDB nimmt einen **Datei-Lock pro Prozess**: zwei *Prozesse* können dieselbe `.duckdb` nicht gleichzeitig read-write öffnen. *Innerhalb* eines Prozesses sind mehrere Connections erlaubt, aber eine einzelne Connection ist nicht nebenläufig benutzbar. Deshalb ist `EventStore` seit dem Parallel-board-loop **thread-sicher**: ein `threading.RLock` serialisiert alle `_conn`-Zugriffe (`store.py`). Der parallele Conductor (`--max-parallel N`) teilt **eine** `EventStore`-Instanz über alle Worker-Threads — Writes serialisieren über den Lock (~ms, vernachlässigbar gegen Minuten-Runs). Echte Subprozess-Parallelität (mehrere `forge run`-Prozesse) ginge wegen des Datei-Locks **nicht** ohne Single-Writer-Daemon oder per-Run-Event-Sinks + Merge — bewusst nicht in dieser Stufe (eigener großer Schritt, v2/M2). Wer parallel arbeitet, reicht den geteilten Store in `execute_run`/`execute_pr_review` über den `store=`-Parameter herein (default `None` = öffnet/schließt selbst, unveränderter Single-Pass-Pfad).

Worktree-Create/Remove (`git worktree add/remove`) nimmt einen kurzen Lock auf die `.git/worktrees`-Metadaten — parallele Adds würden kollidieren. `worktrees.py` serialisiert NUR diese schnelle git-Operation über `_WORKTREE_GIT_LOCK` (prozess-global), nicht den Run.

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
Label, kein zweiter Konfig-Ort.

**Stage-spezifischer Dispatch (verschiedene Teams pro Stage):** Die **Stage**
bestimmt das **Team** und die **Run-Art**. `plan_tick` liefert
`list[DispatchOrder]` (`number` + `stage`), nicht mehr nur `list[int]`.
`stages.IN_PLACE_WORK_STAGES` (= `{design, qa}`) markiert Stages, in denen ein
Team *in-place* arbeitet (kein Stage-Wechsel beim Dispatch, anders als
`ready→in-dev`) und seinen Advance-Auslöser produziert; `advance` schreibt das
Item nächsten Tick fort, sobald das Signal vorliegt. `_run_conductor_watch`
verzweigt nach `DispatchOrder.stage`:
- `design` → `_dispatch_design_run` (architect-Roster, `create_pr=False`, Output
  `PlanProposed` → `has_plan` → `design→ready`),
- `qa` → `_dispatch_review_run` (Review-Merge-Agent, `execute_pr_review`, Output
  `PRReviewed`/`PRMerged` → `has_merged_pr` → `qa→release`; PR-Nummer via
  `conductor.pr_number_for_issue`),
- sonst → der bestehende Dev-Loop (`_dispatch_issues`, PR).

Der QA-Dispatch ist zusätzlich durch `StageSignals.review_done` gegated: ein
bereits gereviewter (aber nicht gemergter, z.B. `request_changes`) PR wird nicht
jeden Tick erneut reviewt — sonst teure Endlosschleife. Neue executable Stage =
Eintrag in `IN_PLACE_WORK_STAGES` **+** Advance-Signal in `StageSignals`/`advance`
**+** Dispatch-Zweig. `requirements`/`release` fehlt jeweils noch ihr
„fertig"-Signal. **Noch offen:** Live-Verifikation der gh-Kommandos gegen ein
echtes Board (bisher nur Stub-getestet); Re-Dispatch/Eskalation eines
`in-dev`-Items, dessen Run keinen PR produzierte; Re-Review nach neuen Commits
auf einem `request_changes`-PR (reaktiv, Inkrement 1c).

Mantra 3: der Heartbeat taktet das Dispatchen von Runs
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

### Live-Tracking: Stream-Log als primäres Signal, Worktree-Poll als Basis

Der propose-Pfad läuft seit dem stream-json-Umbau mit `--output-format
stream-json --verbose`: `_pump_streaming` (claude_cli.py) schreibt **jeden
claude-Event live** als Envelope `{"ts": <UTC-ISO>, "event": <event>}` nach
`.forge/logs/<run_id>/propose-<utc>.jsonl` — geflusht pro Zeile, Timestamps
stammen von forge (claude-Events tragen keine), damit sichtbar ist, WO die
Zeit verbraucht wird. Das Log liegt bewusst AUSSERHALB des Worktrees
(`revert()` macht `git clean -fdx` — bei DISCARD/Timeout braucht man das Log
gerade). Das finale `result`-Event hat exakt die Shape des alten
json-Outputs (`_extract_result_event`) — der restliche propose-Pfad ist
unverändert; verifiziert gegen echtes `claude` (Format + `--verbose`-Pflicht
im Print-Mode).

`forge watch [RUN_ID]` (`watch.py`) rendert daraus das Panel
„agent-aktivität": Tool-Calls mit Kompakt-Detail (Bash-Command, file_path,
Task-`subagent_type` + description), `↳`-Präfix für Subagent-Aktivität (via
`parent_tool_use_id`), Fehler-tool_results, Turn-/Tool-Zähler. Dazu weiterhin
der **lock-freie Worktree-Poll** (git-Status + File-mtimes + `forge:`-Commits)
und die **opportunistische** Event-Chronik aus DuckDB (Single-Writer-Limit —
`_try_read_events` degradiert sauber, kein Lock-Bruch). Alles read-only,
getailt (`_tail_lines`, letzte 512 KB — Logs wachsen über lange Runs);
Kern-Funktionen pure + via injizierte Deps testbar. Mantra 3 intakt:
beobachtet nur, taktet/entscheidet nichts.

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

### Zwei Merge-Wege: GitHub-Auto-Merge (Grauzone) UND Agent-Review-Merge (opt-in)

**Weg 1 — GitHub-Auto-Merge (Spec-Grauzone, unverändert).**
`forge board-loop --auto-merge` und `forge run --auto-merge` rufen
`gh pr merge --auto --squash --delete-branch` auf. Das **queued** den
Merge bei GitHub — der eigentliche Merge passiert server-seitig, von
GitHubs Bots, asynchron, sobald alle required Checks grün sind. forge
selbst führt hier **keinen** synchronen `merge`-Subprozess aus
(`forge_adapters.github.pr.queue_auto_merge`).

**Weg 2 — Agent-Review-Merge (opt-in, forge merged selbst).**
`forge review-pr <N>` lässt einen Agent einen **offenen** PR bewerten
(`PRReviewer` → `agent.review`, fail-closed) und merged ihn **synchron
selbst** (`gh pr merge <N>` ohne `--auto`, `forge_adapters.github.pr.merge_pr`),
WENN alle drei Bedingungen erfüllt sind (`pr_review.decide_merge`, rein):
1. `capabilities.merge_pr` ist opt-in `true` (default `false`),
2. das Review-Verdikt ist `approve` und `score >= threshold`,
3. der CI ist grün (`summarize_ci` == `pass`; `none` nur mit `allow_missing_ci`).
Zusätzlich Konflikt-Guard (`mergeable == CONFLICTING` → nie). Events:
`PRReviewed` (immer) + `PRMerged` (`merged_by_forge=True`) beim Merge.

Das ist die **bewusste Abkehr** vom ursprünglichen kategorischen
`merge_pr=Literal[False]`-Ausschluss (Operator-Entscheidung). `merge_pr`
ist jetzt `bool` in der Spec, der Hard-Deny in `Capabilities.check_action`
gilt nur noch für `push_to_main`/`push_force`. Mantra 3 bleibt intakt:
der Agent **urteilt**, forge **entscheidet/effektiert** deterministisch.

Wer das nicht will: `capabilities.merge_pr` auf `false` lassen (Default) —
dann reviewed `forge review-pr` nur und merged nie.

## Event-Schema — Achtung, irreversibel

`payload_schema_version` ist **pro Kind** versioniert (Spec Teil 4.1). Wenn du ein bestehendes Sub-Schema änderst:

- Additive Änderungen (neues optionales Feld) → `1.0` → `1.1`, alte Events lesen weiterhin
- Breaking Änderungen → eigentlich nicht erlaubt in v1, weil historische Daten nicht migriert werden
- Neuer EventKind → neue Datei in `events/kinds/`, `register_payload(...)` aufrufen

Vor jeder Schema-Änderung: `len(EventKind) == 23` und `len(_PAYLOAD_REGISTRY) == 23` testen (v0.4 = v0.3-17 + `ISSUE_TRIAGED` = 18; + Loop 2: `ConductorTickCompleted`/`WorkItemStageChanged`/`WorkItemBlocked` = 21; + Resilienz: `RunResumeScheduled` = 22; + Agent-Review-Merge: `PRReviewed` = 23).

`PRMerged` steht auf Schema **1.1** (additiv: `merged_by_forge` + `merge_method` für forge-initiierte Merges). Alte 1.0-Events lesen weiter (Defaults).

`PlanProposed` steht auf Schema **1.1** (additiv: `subtasks: list[PlanSubtask]` + `agents_used: list[str]`). Alte 1.0-Events lesen weiter, weil beide Felder Defaults haben — siehe `test_plan_proposed_schema_is_v1_1_with_additive_fields`.

`RunFinished` (1.1: Decision `rate_limited`), `ProposalReceived` (1.1: `session_id`) und `ConductorTickCompleted` (1.1: `scheduled_resume_count`) sind additiv gebumpt für die Session-Limit-Resilienz (s. u.).

## CodingAgent ist Plug-in, nicht Fundament

`ClaudeCodeCLIAgent` ist eine konkrete Implementierung. Das `CodingAgent`-Protocol erlaubt morgen einen `CodexCLIAgent`, `OpenCodeAgent` oder `DirectAnthropicAPIAgent`. Wenn du im Runner gegen `claude` direkt programmierst statt gegen das Protocol, brichst du das.

### Multi-Agent ist Plug-in-intern, nicht Runner-Sache

Das Subagent-Team (architect → developer → tester → reviewer) wird **vom Master-`claude` orchestriert**, nicht vom Runner. Der Runner ruft ein einziges `propose()` auf; die Rollen-Choreografie lebt komplett im `ClaudeCodeCLIAgent`. Würde der Runner die Schritte selbst takten, müsste die Loop die Agent-Rollen kennen — das bricht Mantra 3 und das Plug-in-Prinzip.

**Parallele Subtasks (intra-Run, Plug-in-intern).** Der Orchestrator-Prompt (`build_orchestrator_prompt`) weist den Master an, **unabhängige** Subtasks (disjunkte Files, keine Daten-Abhängigkeit) **parallel** zu dispatchen — mehrere `developer`-Task-Calls in EINER Nachricht — und nur abhängige/Datei-teilende Subtasks zu serialisieren. Sicherheits-Leitplanke im Prompt (`Parallel safety`): alle Subagents teilen EINEN Worktree, also nie zwei Developer auf dieselbe Datei in einem Batch (Lost-Update-Race); im Zweifel serialisieren. Das ist die zweite Parallelitäts-Ebene neben dem parallelen Conductor (`--max-parallel`, Run-Ebene): hier laufen Rollen *innerhalb* eines Runs nebenläufig. Bewusst eine reine Prompt-Instruktion — der Runner taktet die Subagents nicht (Mantra 3), Claudes Task-Tool führt die parallelen Calls aus.

Der **reviewer** ist das opt-in 4. Arbeitspferd (`KNOWN_AGENTS`, aber **nicht** in `DEFAULT_AGENTS`): read-only (`Read, Glob, Grep, Bash`), läuft als LETZTER Schritt nach der Tester-Verifikation und liest den kumulativen Diff kritisch gegen (Korrektheit, Surfaces/Forbidden, Security, Test-Qualität). Er editiert nichts — BLOCKING-Findings gehen zurück an den Developer (zwei Runden max), dann re-verifiziert der Tester. Damit bleibt Mantra 3 intakt: der reviewer urteilt, er entscheidet nicht und schreibt keinen Code. **Abgrenzung zum Judge:** Der Judge (`evaluators/judge.py`, `review()`) ist die *fail-closed, gescorte* Loop-Phase, die `llm_judge_score` in ein Gate füttert und keep/discard mitentscheidet — read-only, **außerhalb** der Orchestrierung. Der reviewer ist *in-Orchestrierung*, qualitativ, und verbessert den Diff *bevor* er den Worktree verlässt. Sie sind komplementär, nicht redundant.

Welche Rollen mitwirken, steuert die `agents:[...]`-Roster aus der Trigger-Config (`spec.triggers.on_issue_label[label].agents`). Der board-loop löst das Roster pro Issue-Label auf (`_roster_for_issue`), `forge run` nimmt `--agents a,b,c` (oder `--multi-agent` als Shortcut fürs Default-Roster). Das Roster fließt in:
- `ClaudeCodeCLIAgent(agents=...)` → `build_orchestrator_prompt(roster)` baut den `--append-system-prompt` aus genau den aktiven Rollen; ohne `architect` entfallen Plan-Schritt + `---FORGE-PLAN-...---`-Marker, ohne `tester` die Verifikations-Schritte, ohne `reviewer` der Review-Schritt + die BLOCKING-Findings-Schleife. `_install_subagents(wt, agents=roster)` kopiert nur die Roster-`.md`s in den Worktree.
- `RunConfig.agents` ist das **konfigurierte** Roster. `PlanProposed.agents_used` trägt dagegen die **tatsächlich gerufenen** Rollen: der Master meldet sie im `---FORGE-AGENTS-BEGIN/END---`-Block (`extract_agents_from_master_output`), der Runner setzt `agents_used = agents_invoked or list(config.agents)` — Selbstauskunft gewinnt, Config ist nur Fallback. Gleicher Trust-Model wie die Plan-Checkboxen: best-effort, aber misst Wirklichkeit statt Absicht (Mantra 1). Kein Schema-Bump — das Feld bedeutete schon immer „mitgewirkt".

### Modell pro Rolle: via Projekt-Override

Die `.md`-Templates tragen `model: sonnet` im Frontmatter. Wer eine Rolle auf ein anderes Modell heben will (z.B. `architect` auf opus), legt ein Projekt-Override unter `<repo>/.forge/agents/<role>.md` mit eigenem Frontmatter ab — `_install_subagents` kopiert die ganze Datei (inkl. `model:`-Zeile) über den Default. Das funktioniert in BEIDEN Pfaden (`forge run` + board-loop), weil beide durch denselben Hybrid-Lookup gehen. Kein Spec-Feld nötig, kein zweiter Konfig-Ort. **Achtung:** ein Override OHNE `model:`-Zeile erbt das Master-Modell (oft opus) — in einem realen Run war genau das der größte Kostentreiber (~239K Opus-Output-Tokens ≈ $19). `_install_subagents` warnt via logging, wenn die Zeile fehlt.

### Headless- & Kosten-Disziplin im propose-Pfad

Drei gemessene Kostentreiber orchestrierter Runs (Analyse eines $19/57-min-Runs: 93 % der Wall-Zeit reine API-Inferenz, 13,7 Mio Cache-Read-Tokens durch kalt startende Subagents, die dieselben Files 9-11× lasen) sind adressiert:

- **`--disallowedTools AskUserQuestion`** steht in beiden claude-Aufrufen (`propose` + Judge-`review`; `HEADLESS_DISALLOWED_TOOLS` in `claude_cli.py`). Interaktive Fragen werden im Print-Mode ohnehin auto-denied, kosten aber einen vollen API-Round-Trip — und `--allowedTools` allein schließt unter `bypassPermissions` nichts aus. Architect-Template + Orchestrator-Prompt sagen stattdessen: spec-treuen Default wählen und unter `Design decisions` dokumentieren. Der `Insufficient context`-Output bleibt der Weg bei fundamental fehlendem Kontext (finales Output, keine interaktive Frage).
- **Context handoff** (Orchestrator-Prompt, Sektion „Context & cost discipline"): Subagents starten mit leerem Kontext — der Master pastet Memory-Auszug, bekannte Datei-Pfade und (für den developer) den Subtask-Block in jeden Task-Prompt, statt Subagents breit re-scannen zu lassen.
- **Targeted verification**: pro Subtask nur gefilterte Tests / inkrementelle Builds (à la `--no-build`); die volle Suite läuft genau einmal, im finalen Tester-Schritt. Steht im Orchestrator-Prompt UND in den developer/tester-Templates.

### Project memory: Erledigt-Status kommt aus dem Event-Strom

`_project_memory.py` baut den Memory-Block aus drei Quellen: Operator-Seed (`.forge/memory.md`), **Recent run outcomes** (`RunStarted` + `RunFinished` + `PRMerged`, korreliert über `run_id`) und jüngsten Plan-Summaries. Die Outcomes existieren, weil der Seed zwangsläufig veraltet: `.forge/**` ist für den Agenten Forbidden Zone, forge selbst schreibt `memory.md` NIE (Operator-Datei) — ein „WP4 noch offen" im Seed bleibt also stehen, obwohl der PR längst gemerged ist. Der Addendum-Text legt fest: bei Widerspruch gewinnen die Run-Outcomes, nicht die Seed-Prosa. Wenn du hier erweiterst: Memory ist read-only-Auswertung des Event-Stroms (Mantra 3), keine neue Event-Logik.

### Was die Orchestrierung (noch) NICHT misst

- **Per-Subagent-Telemetrie** (Cost/Tokens/Turns pro Rolle) als *Events/Metriken*: das `result`-Event liefert nur Master-Totals. Die **Rohdaten** liegen seit dem stream-json-Umbau vollständig in `.forge/logs/<run_id>/*.jsonl` (jeder Subagent-Event trägt `parent_tool_use_id`) — `forge watch` zeigt sie live, aber eine Aggregation in den Event-Store (z.B. `cost_per_role`) ist bewusst noch offen: das wäre neue Event-Logik und braucht einen eigenen, kleinen Schritt.
- **„Zwei Runden max"** bei roter Verifikation/BLOCKING-Findings ist bewusst eine Prompt-Instruktion, **kein** vom Runner gezähltes Limit: die harte Ressourcen-Grenze sind Cost-Caps + `max_turns` (`_check_run_cost_cap`). Würde der Runner die Retry-Runden zählen, müsste er die Orchestrierungs-Schritte kennen → Mantra-3-Bruch.

Ein einsamer `["developer"]` braucht keine Orchestrierung (`roster_needs_orchestration`) — das ist der klassische Single-Agent-Run (kein Task-Tool, kein Plan). Das alte `multi_agent: bool` bleibt rückwärtskompatibel (`True` = Default-Roster, `False` = `["developer"]`).

### Session-Limit-Resilienz: erkennen → sichern → fortsetzen

Läuft ein orchestrierter Run mitten in der Arbeit in ein Claude-Usage-/Session-Limit, ist das **kein** Fehlschlag, sondern ein **fortsetzbarer** Zustand. Das Signal ist tückisch: claude meldet es als `result`-Event mit `subtype: "success"` ABER `is_error: true` + `api_error_status: 429` und Text `"You've hit your session limit · resets <zeit>"`. Erkennung darum NICHT über `subtype`/returncode, sondern in `claude_cli._is_rate_limited` (`is_error` + 429, Text-Fallback). Drei Schichten, strikt getrennt (Mantra 3):

1. **Erkennen & sichern (Loop 1, `claude_cli` + `runner`).** `propose()` wirft `CodingAgentRateLimited` (trägt `session_id` aus dem stream-json init-Event, `reset_at` via `_parse_reset_time` → nächste UTC-Occurrence der genannten Lokalzeit, echten `cost_usd`). Der Runner fängt sie **vor** `CodingAgentError`, bucht den echten Cost ein (sonst $0-Bug), beendet den Run als Decision **`rate_limited`** und ruft `_finish_rate_limited`: Partial-Arbeit als `forge: WIP`-Commit (`allow_empty=True`) sichern, **kein** `revert()`/`git clean -fdx`, Branch + Worktree bleiben. Anker als **`RunResumeScheduled`**-Event (original_run_id, resume_session_id, resume_at, resume_worktree, wip_commit, issue_number). `ProposalReceived` trägt jetzt `session_id` (Schema 1.1).
2. **Fortsetzen (Loop 1 + CLI).** `forge run --resume <run_id>` lädt den Anker (`_load_resume_anchor`), der Runner dockt via `worktrees.attach()` an den eingefrorenen Worktree an (`base_commit` = WIP-HEAD, nicht Run-Start) und ruft `propose(resume_session_id=…)` → `claude --resume <id>` setzt die Session mit vollem Kontext fort. Ein übergebener `run_id` (statt frisch generiertem ULID) IST das Resume-Signal (`SequentialRunner(run_id=…)` → `_is_resume`). `resume_session_id` ist ein optionaler Param am `CodingAgent`-Protocol — Plug-in-safe, Agents ohne Resume ignorieren ihn.
3. **Auto-warten & dispatchen (Loop 2 / Conductor).** Der Runner plant den Resume **nie selbst**. `conductor.derive_pending_resumes(events, now)` leitet fällige Resumes **rein aus dem Event-Strom** ab (analog `derive_signals`): jüngster `RunResumeScheduled` pro `run_id`, dessen `resume_at <= now` und der seither **kein** neues `RunStarted` (gleiche run_id, `ts >` Anker) hat — at-most-once, weil der synchrone Resume-Dispatch sofort ein frisches `RunStarted` derselben run_id emittiert. `resume_at == None` (Reset-Zeit unparsebar) = **nur manuell**, nie auto. Der board-loop-Conductor-Tick (`_run_conductor_watch` → `_dispatch_resume`) feuert fällige Resumes über denselben `execute_run --resume`-Pfad, zählt sie in `ConductorTickCompleted.scheduled_resume_count` (Schema 1.1).

`tzdata` ist Pflicht-Dependency von forge-execute (Windows liefert keine IANA-DB; ohne sie wäre `reset_at` immer `None` → kein Auto-Resume). Tests: `test_rate_limit_resume.py` (Erkennung + Runner-Pfad + End-to-End-Resume), `test_conductor_resume.py` (pure Ableitung).

## Sicherheit — vier Schichten

1. **Forbidden Zones** (Pfad-Ebene) — `Capabilities.check_edit/read`
2. **Capabilities** (Aktions-Ebene) — `Capabilities.check_action/run/egress`
3. **Cost-Caps** (Ressourcen-Ebene) — `SequentialRunner._check_run_cost_cap`
4. **Subprocess-Isolation** (Prozess-Ebene) — Worktree pro Run, separate venv kommt in M2

`push_to_main` / `push_force` sind dreifach gesichert: in der Spec via `Literal[False]`, in `Capabilities.check_action` als hartkodiertes Deny, und im PR-Body steht der Hinweis explizit.

`merge_pr` ist **nicht** mehr hart-deny (Agent-Review-Merge): `bool` in der Spec, gefolgt von `Capabilities.check_action`. Die Sicherheit liegt hier in der **Mehrfach-Bedingung** vor dem Merge (`pr_review.decide_merge`): opt-in-Capability **und** Agent-`approve` **und** `score >= threshold` **und** grüner CI **und** kein Merge-Konflikt. Fail-closed: scheitert der Review-Agent, gilt `request_changes`/`0.0` → kein Merge.

## Wenn du nicht sicher bist

- **Eine neue Mutation einführen?** Trag sie in `forge_core.events.kinds.mutation.py` als neuen `MutatorKind` ein, schreibe ein eigenes Modul in `forge_execute/mutators/`, registriere im Runner.
- **Eine neue Eval-Suite?** Erweitere `EvalSuiteConfig.parses` um den neuen Modus, schreibe einen Parser in `evaluators/command.py`, hänge ihn an `_parse()` an.
- **Eine neue Capability?** Erweitere `CapabilitiesConfig`, dann `Capabilities`-Klasse, dann Tests doppelt schreiben — Capabilities sind die wichtigste Verteidigungslinie.

Wenn dein Change ein bestehendes Modul „etwas größer" macht statt klar zuordenbar zu sein: stop, denk nach, frag den Operator. Das ist meistens ein Anzeichen, dass die Schichtung verletzt würde.
