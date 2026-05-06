# forge — M1 Fortschritts-Checkliste

> Lebende Datei. Wir haken ab, was fertig ist; bei Status-Änderung committen.
> Quelle: `docs/forge-spec-v0.2.md` Teil 9 (MVP) + `docs/todos.txt` (5 Schritte).

**Erfolgsdefinition M1** (Spec Teil 9): Auf PINTA läuft `forge run --strategy
sequential --focus legacy_test_revival` über Nacht und produziert einen
mergebaren PR. Alle Events sind im Store, alle Artefakte im Blob-Store, Replay
funktioniert.

**Übergangs-Erfolg M1 → Phase 2** (Spec Teil 12.2): >50% der Auto-Fix-PRs
werden ohne Edits gemerged.

---

## Schritt 1 — Repo + Skeleton (Tag 1)

- [x] uv-Workspace `pyproject.toml` mit 4 Packages
- [x] `packages/forge-core` Stub + `pyproject.toml`
- [x] `packages/forge-execute` Stub + `pyproject.toml`
- [x] `packages/forge-cli` Stub + `pyproject.toml`
- [x] `packages/forge-adapters` Stub + `pyproject.toml`
- [x] README mit North-Star-Satz
- [x] CHANGELOG.md angelegt
- [x] `uv sync` läuft durch, pytest sammelt 0 Tests ohne Beschwerde
- [x] `examples/pinta/.forge/project.yaml` als Reference-Spec
- [x] `.gitignore` um forge-Runtime-Pfade erweitert

**Commit:** `3b3b1b6` (Initial scaffolding + forge-core)

---

## Schritt 2 — forge-core (Woche 1)

### 2a. `events.py` — Event-Schema + Sub-Schemas

- [x] `Event`-Klasse (Pydantic v2, frozen, extra=forbid)
- [x] `EventKind` StrEnum mit allen 16 v1-Kinds
- [x] Pro Kind ein eigenes Payload-Sub-Schema in `events/kinds/`
- [x] Pro Kind eigene `payload_schema_version` (Spec Teil 4.1)
- [x] Registry + `register_payload()` mit Doppelregistrierung-Check
- [x] `build_event()` validiert Payload gegen Sub-Schema
- [x] Artefakt-Hashes auf `sha256:<64-hex>` validiert
- [x] Timestamps müssen tz-aware UTC sein
- [x] ULID als event_id (sortierbar)
- [x] Tests: alle 16 Kinds registriert, Roundtrip, Frozenness

### 2b. `blobs.py` — Content-Addressed Storage

- [x] `BlobStore` mit Layout `<root>/<hash[:2]>/<hash>`
- [x] `put(bytes)`, `put_text()`, `get()`, `get_text()`, `has()`
- [x] Atomare Writes via `tempfile` + `os.replace`
- [x] Deduplication (gleicher Hash = gleicher Blob)
- [x] `gc(keep=..., older_than_days=90)` mit `GCStats`
- [x] `iter_blobs()` für Wartung
- [x] Tests: Roundtrip, Unicode, Random-Bytes, GC mit Backdate

### 2c. `store.py` — DuckDB-Sink

- [x] `events`-Tabelle mit JSON-Payload + häufig gefilterten Spalten
- [x] Vier Indizes (run_id, kind, ts, project)
- [x] View `runs_with_outcomes`
- [x] View `cost_per_focus`
- [x] View `pr_merge_rate_by_focus`
- [x] View `top_failure_modes`
- [x] `append()`, `append_many()` mit Idempotenz auf event_id
- [x] `events_for_run()`, `events_by_kind()`, `query()`, `count()`
- [x] `referenced_artifact_hashes()` für GC-Integration
- [x] Context-Manager-Support
- [x] Tests: Roundtrip mit nested Payloads, Views, Idempotenz

### 2d. `spec.py` — project.yaml Loader

- [x] Pydantic-Modelle für komplette Spec-Struktur
- [x] `merge_pr/push_to_main/push_force` als `Literal[False]` hardcoded
- [x] Überlappende Surface-Pfade abgelehnt
- [x] `forbidden`-in-Surface abgelehnt
- [x] `yaml-keys`-Surface erfordert `allowed_keys`
- [x] Score-Weights auf 1.0 normalisiert (mit Warning)
- [x] Eval-Suite-References aus gates/scores/diagnostics geprüft
- [x] `eval_suites` muss `quick` enthalten
- [x] Cost-Caps müssen monoton sein
- [x] Gate braucht `threshold` ODER `max_increase`
- [x] `surface_for_path()` Lookup-Helper
- [x] `load_spec()` + `dump_spec()`
- [x] Tests für jede scharfe Validierung

### 2e. `replay.py` — Skelett

- [x] `RunReconstruction` und `GenerationReconstruction` Dataclasses
- [x] `reconstruct_run(run_id, store, blobs)` API
- [x] Convenience-Properties: `proposal_prompt`, `diff`, `eval_stdout`
- [x] Fehlende Blobs werden geskippt (nicht erzwungen)
- [x] Tests: Prompt+Diff-Roundtrip, leerer Blob-Store, started/finished_at

### 2f. Tests

- [x] `test_blobs.py` (9 Tests)
- [x] `test_events.py` (10 Tests)
- [x] `test_store.py` (8 Tests)
- [x] `test_spec.py` (15 Tests)
- [x] `test_replay.py` (4 Tests)
- [x] `test_e2e.py` (1 Test: 5 Events anlegen, queryen, reconstruct)
- [x] **47 Tests grün, ruff clean**

**Commit:** `3b3b1b6` (Add forge-core)

---

## Schritt 3 — forge-execute minimal (Woche 2)

### 3a. `worktrees.py` — Git-Worktree-Pool

- [x] `create(run_id) -> Path`
- [x] `cleanup(path)`
- [x] `apply_patch(path, diff)`
- [x] `revert(path)`
- [x] `commit(path, message)`
- [ ] Separate venv pro Worktree (Optimierung; v1 reicht eine Repo-venv)
- [x] Zusatz: `diff_against_base`, `changed_files`, `has_changes`
- [x] Tests: 3 parallele Worktrees, sauberes Cleanup, apply+revert Roundtrip (10 Tests)

### 3b. `capabilities.py` — Capability-Enforcement

- [x] `Capabilities`-Klasse mit `check_edit/read/run/action/egress`
- [x] Pfad gegen Surfaces+Forbidden prüfen (gitignore-Glob via pathspec)
- [x] Aktion gegen Capability-Listen prüfen
- [x] `CheckResult.to_violation()` für Event-Payload-Konvertierung
- [x] Glob-Matching für Pattern wie `pytest *`, `**/*.py`, `backend/migrations/**`
- [x] `merge_pr`/`push_to_main`/`push_force` immer abgelehnt (Defense-in-depth)
- [x] `allowed_tools_string()` für Claude-CLI-Übersetzung
- [x] Tests: Surfaces, Forbidden, Capabilities, Egress, Tool-String (19 Tests)

### 3c. `mutators/code.py` — Code-Mutator

- [x] Unified-Diff via `git apply` anwenden
- [x] Capability-Pre-Check für alle betroffenen Pfade
- [x] `git diff --check` (Whitespace)
- [x] Syntax-Check via `ast.parse` für .py-Files
- [x] Auto-Revert bei Syntax/Whitespace-Failure
- [x] Idempotenz: apply + revert == identity
- [x] `extract_changed_paths`, `count_diff_lines` Helpers
- [x] Tests: 11 Tests inkl. malformed Diff, syntax error, forbidden path
- [ ] Edit-Operations als alternative Eingabe (M2)

### 3d. `evaluators/command.py` — Command-Evaluator

- [x] Shell-Command mit `budget_s`-Timeout ausführen
- [x] stdout als JSON oder pytest-Output parsen (`pytest_json`, `scores_json`, `raw`)
- [x] `tool_versions`-Artefakt erzeugen (python, pytest, ruff, mypy, node, npm, git)
- [x] Exit-Code via `EvalRunResult.success`
- [x] Process-Tree-Termination auf Windows via `taskkill /T /F`, POSIX via `os.killpg`
- [x] Tests: 16 Tests inkl. echtes pytest, partial pass, Timeout-Killing, JSON-Parser

### 3e. `gates.py` + `scoring.py` — Gates und Composite

- [x] `evaluate_gates(measurements, spec, baseline) -> (passed, list[GateResult])`
- [x] `compute_composite(measurements, spec, baseline) -> float | None`
- [x] Composite nur, wenn alle Gates grün (Caller-Vertrag)
- [x] `keep_or_discard(new, baseline, tolerance)` aus Spec Teil 6.5
- [x] `max_increase`-Gates mit Baseline-Logic
- [x] Tests: 23 Tests, deckt Gates+Scores+Decision-Logic ab

### 3f. `runner.py` — Sequential Runner

- [x] `SequentialRunner.run() -> RunResult` mit `RunConfig`-Eingabe
- [x] Phasen: Propose → Mutate-Validate → Preflight → Eval → Decide
- [x] Cost-Cap-Check pro Generation und pro Run; `CostCapHit`-Events
- [x] `RunStarted`/`RunFinished`-Events mit baseline_metrics, decision, total_cost
- [x] `GenerationStarted`/`GenerationFinished`-Events
- [x] Bei `KEEP`: commit auf Run-Branch mit strukturierter Message; Baseline-Update
- [x] Bei `DISCARD`: `worktree.revert()`
- [x] Capability-Verletzung → `GuardrailViolation`-Event + Run-Abort
- [x] Preflight-Failure → `PreflightFailed`-Event + Discard
- [x] Score-Delta-Logic erweitert: rot→grün-Übergang ist immer Improvement
- [x] Tests: 3 End-to-End Szenarien (red→green, no-op, capability-violation)

### 3g. `agents/` — CodingAgent-Interface

- [x] `CodingAgent`-Protocol (`propose` mit Worktree-Path, Capabilities, Budget)
- [x] `ClaudeCodeCLIAgent` (Subprozess via `claude -p --output-format json`)
- [x] `MockCodingAgent` mit static_result / sequence / callable_ Modi
- [x] Übersetzung `capabilities.run` → `--allowedTools` Bash(...)-Patterns
- [x] Defensive Fehlermeldung bei fehlendem `claude`-Binary oder API-Key
- [ ] `review` und `estimate_cost` (M2)
- [x] Tests: 9 Tests, MockCodingAgent für Runner-Integration

**Erfolgskriterium Schritt 3 erreicht:** `test_runner_red_test_to_green_pr`
beweist End-to-End Funktion. Mini-Repo mit rotem Test → MockCodingAgent fixt
calc.py → Runner führt 5 Phasen aus → KEEP, commit auf forge/&lt;run_id&gt;-Branch,
alle 11 erwarteten Event-Kinds im DuckDB-Store, Prompt+Diff im Blob-Store.

---

## Schritt 4 — forge-cli + GitHub-Adapter (Woche 3)

### 4a. `forge-cli`

- [ ] `forge run --spec <path> --trigger-type schedule --focus <name>`
- [ ] `forge analyze` — drei Standard-Reports als Markdown
- [ ] `forge doctor` — Spec-Konsistenz, Tool-Verfügbarkeit, API-Key
- [ ] `forge replay <run_id>` — lesbare Markdown-Timeline
- [ ] `forge sync-claude-md` — AUTOGENERATED-Blöcke aus project.yaml regenerieren
- [ ] `forge init` — Template-Spec aus erkanntem Stack
- [ ] typer-basiert, Rich für Output

### 4b. `forge-adapters/github/`

- [ ] GitHub Action Templates (Issue, PR, CI-Failure, Schedule)
- [ ] `gh pr create`-Wrapper mit strukturiertem Body (Score-Trend, Run-Summary-Link)
- [ ] Webhook-Listener für `pull_request.merged` → `PRMerged`-Event
- [ ] Webhook für `PRReverted`-Erkennung

**Erfolgskriterium Schritt 4:** `forge run` produziert lokal einen PR. Issue mit
Label `auto-fix` auf Test-Repo löst Workflow aus, PR entsteht.

---

## Schritt 5 — PINTA-Integration (Woche 4)

- [ ] `.forge/project.yaml` aus `examples/pinta/` in PINTA committen
- [ ] `backend/scripts/run_full_eval.sh` — JSON-Output aller Scores
- [ ] Judge-Skript (oder erstmal weglassen)
- [ ] Erste Smoke-Runs: `forge run --max-iterations 3`
- [ ] GitHub Actions Workflows aktivieren, Secrets setzen
- [ ] Erstes Issue mit `auto-fix`-Label → Auto-PR

**Erfolgskriterium M1:** Drei automatisch generierte PRs auf PINTA innerhalb
einer Woche, mindestens einer ohne menschliche Edits gemerged.

---

## Querschnittsthemen (begleitend)

### Sicherheit (Spec Teil 7)

- [ ] Forbidden-Zone-Verletzung → `GuardrailViolation` + Run-Abort
- [ ] Capability-Enforcement im CLI-Subprozess via `--allowedTools`
- [ ] `.claude/hooks/` PreToolUse-Hook im Worktree als zweite Verteidigungslinie
- [ ] Egress-Kontrolle: nur whitelisted Hosts
- [ ] Prompt-Injection-Wrapping für untrusted Daten (Issue-Bodies, Eval-Output)

### Operations (Spec Teil 8)

- [ ] Issue-Templates in `.github/ISSUE_TEMPLATE/`
- [ ] Run-Summary-Markdown pro Run in `.forge/runs/`
- [ ] Eskalation auf Cost-Cap, 3× erfolgloser Schedule-Lauf, Capability-Violation

### CLAUDE.md-Lifecycle (todos.txt Q4)

- [ ] CLAUDE.md im `forbidden`-Set
- [ ] AUTOGENERATED-Blöcke via `forge sync-claude-md`
- [ ] Pre-Commit-Hook + CI-Drift-Check
- [ ] `forge doctor --validate-claude-md`

---

## Out-of-scope (v2/v3)

Ausdrücklich **NICHT in M1** (Spec Teil 9):

- Population-Based Search
- Bandit, Bayesian Optimization, Loop 3
- `factory_state.yaml`, Shadow-Mode
- Modell-Routing
- Container-Sandbox
- Zentrale Aggregation (S3/Azure Parquet)
- Auto-Merge — kategorisch ausgeschlossen v1
