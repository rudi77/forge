# Changelog

Alle bemerkenswerten Änderungen an forge werden hier dokumentiert. Format: [Keep a Changelog](https://keepachangelog.com/), Versionierung: [SemVer](https://semver.org/).

## [Unreleased] — M1 in progress

163 Tests grün, ruff clean. Loop-1-Kernpfad funktioniert End-to-End:
roter Test → MockCodingAgent fixt → Runner committet auf `forge/<run_id>`-Branch
mit allen 11 v1-Event-Kinds im Store und Prompt+Diff im Blob-Store.

### Hinzugefügt

#### Schritt 4 — `forge-cli` + GitHub-Adapter (`dbb1ebe`)

- `forge run` — typer-CLI mit `--focus / --prompt / --trigger / --max-iterations / --create-pr / --dry-run`
- `forge analyze` — vier Markdown-Reports (recent runs, cost/focus, PR-Merge-Rate, Top Failure-Modes)
- `forge doctor` — fünf Check-Kategorien (Spec, Tools, Claude-CLI, API-Key, Forbidden-Sanity)
- `forge replay <run_id>` — Run-Timeline mit aufgelösten Prompt+Diff-Artefakten
- Auto-Discovery der Spec via aufwärts-Suche ab CWD
- Built-in Prompt-Templates für `legacy_test_revival`, `lint_cleanup`, `type_errors_reduction`
- `forge-adapters/github`: `push_branch`, `create_pr_for_run`, `render_pr_body` (Score-Trend, Files, Diff-Excerpt)
- Webhook-Wrapper `record_pr_merged` / `record_pr_reverted` + `find_run_id_for_pr`
- Vier GitHub Action Templates (Issue, CI-Failure, Nightly, PR-Merged) mit
  UNTRUSTED-USER-CONTENT-Wrapping nach Spec Teil 7.3

#### Schritt 3 — `forge-execute` Loop 1 (`4afe496`, `6b6630b`)

- `gates.py` — `evaluate_gates` mit `threshold` und `max_increase`-Modi, Baseline-Logic
- `scoring.py` — gewichteter Composite, `keep_or_discard` mit rot→grün-Improvement-Logic
- `capabilities.py` — pathspec-basiertes Glob-Matching, `merge_pr/push_to_main/push_force` als `Literal[False]` hardcoded, `allowed_tools_string()` für Claude-CLI
- `worktrees.py` — `WorktreeManager` mit `create / cleanup / apply_patch / revert / commit`
- `mutators/code.py` — Capability-Pre-Check, `git apply`, AST-Syntax-Check, Auto-Revert bei Failure
- `evaluators/command.py` — Shell-Command mit `budget_s`-Timeout, `pytest_json` / `scores_json` / `raw` Parser, robuste Process-Tree-Termination (Windows: `taskkill /T /F`, POSIX: `os.killpg`), `tool_versions`-Artefakt
- `agents/` — `CodingAgent`-Protocol, `ClaudeCodeCLIAgent` (Subprozess via `claude -p`), `MockCodingAgent` mit static / sequence / callable Modi
- `runner.py` — `SequentialRunner` mit fünf Phasen, Cost-Caps pro Generation und Run, alle 11 v1-Event-Kinds, Baseline-Tracking pro KEEP

#### Schritt 2 — `forge-core` (`3b3b1b6`)

- `events/` — `Event`-Klasse (Pydantic v2 frozen, ULID-IDs, UTC-only ts), 16 Sub-Schemas mit eigener `payload_schema_version`, Registry mit Doppelregistrierungs-Schutz
- `blobs.py` — Content-Addressed Storage mit atomaren Writes, Dedup, GC mit `keep`-Set
- `store.py` — DuckDB-Sink mit `events`-Tabelle + 4 Standard-Views (`runs_with_outcomes`, `cost_per_focus`, `pr_merge_rate_by_focus`, `top_failure_modes`), `referenced_artifact_hashes` für GC-Integration
- `spec.py` — `project.yaml`-Loader mit harten Validierungen (`merge_pr/push_to_main/push_force=false` hardcoded, überlappende Surfaces verboten, `forbidden`-in-Surface verboten, Score-Weights normalisiert, Eval-Suite-References geprüft, monotone Cost-Caps)
- `replay.py` — `reconstruct_run` API mit Artefakt-Auflösung aus Blob-Store

#### Schritt 1 — Skeleton (`3b3b1b6`)

- uv-Workspace mit vier Packages (`forge-core`, `forge-execute`, `forge-cli`, `forge-adapters`)
- Reference-Spec `examples/pinta/.forge/project.yaml`
- Spec v0.2 als `docs/forge-spec-v0.2.md`
- Fortschritts-Checkliste `docs/progress.md` (`0703f27`)

### Designentscheidungen / Notes

- **CRLF-Warnings beim Commit** sind harmlos — Windows-Default für Git, Inhalte bleiben identisch
- **DuckDB single-row INSERT ist langsam (~12 ms each)** auch in-memory; in-Produktion irrelevant (~50 Events pro Run = ~0.6 s), Migration auf Arrow-Bulk wenn nötig
- **`tests/__init__.py` aus allen Packages entfernt** — sonst kollidieren gleichnamige Test-Module zwischen Packages bei pytest-Discovery
- **CodingAgent ist Plug-in, nicht Fundament** — `ClaudeCodeCLIAgent` ist eine konkrete Implementierung; das `CodingAgent`-Protocol ermöglicht spätere Alternativen ohne Core-Änderung
