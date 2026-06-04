# Changelog

Alle bemerkenswerten Änderungen an forge werden hier dokumentiert. Format: [Keep a Changelog](https://keepachangelog.com/), Versionierung: [SemVer](https://semver.org/).

## [Unreleased] — v0.5: LLM-Judge Verifikationsphase

322 Tests grün, ruff clean.

### Hinzugefügt

#### Judge — opt-in Verifikation gegen Akzeptanzkriterien

Schließt die Lücke zwischen "messbare Wartung" und "Feature-Implementierung":
Für Feature-Issues, die der Mensch in Prosa beschreibt, gibt es keine
natürliche numerische Metrik. Die rein Composite-getriebene Decide-Phase
würde eine reine Feature-Implementierung mangels Score-Verbesserung
verwerfen. Der Judge bewertet den Diff gegen die Akzeptanzkriterien
(= Issue-Text) und liefert einen ``llm_judge_score`` ∈ [0, 1].

- **`JudgeConfig`** (`forge-core/spec.py`) — neuer ``judge:``-Block in
  `project.yaml`, default-konstruiert (opt-in, ``enabled: false``).
  Felder: ``enabled``, ``model``, ``max_turns``, ``threshold``,
  ``budget_s``. Strict-validated, additiv (kein Breaking Change).
- **`llm_judge_score` als `GateKind`** — additive Literal-Erweiterung.
  War bereits `DiagnosticKind`; jetzt auch als Gate nutzbar, wodurch der
  Judge-Score die **unveränderte** keep/discard-Logik bindet. Vor der
  Implementierung fehlt der Messwert → Gate rot; nach Bestätigung durch
  den Judge → grün → bestehender ``gate_revival``-Pfad behält die Änderung.
- **`CodingAgent.review()` + `ReviewResult`** (`agents/base.py`) — neue
  read-only Protocol-Methode. Implementiert in `ClaudeCodeCLIAgent`
  (eigener `claude -p`-Aufruf, read-only Tool-Set, JSON-Verdict) und
  `MockCodingAgent` (static/sequence/callable + default-pass).
- **`JudgeEvaluator`** (`evaluators/judge.py`) — dünne, **fail-closed**
  Schicht über `agent.review()`: bei Crash/Timeout/JSON-Garbage gilt
  ``score=0.0`` / ``fail`` → Gate bleibt rot → DISCARD. Der Judge darf
  ein KEEP nie erzwingen, nur erlauben.
- **Runner-Phase 4b** (`runner.py`) — opt-in Judge zwischen Eval und
  Gate-Auswertung. Mergt ``llm_judge_score`` in die Measurements, bevor
  Gates/Composite ausgewertet werden. Eigenes
  ``EVAL_STARTED``/``EVAL_FINISHED``-Paar mit ``eval_mode="judge"``
  (kein neuer EventKind — `EvalMode` enthielt "judge" bereits),
  Begründung als Blob-Artefakt, Kosten in Cost-Caps.
- **CLI** — `forge run --acceptance-file`, `board-loop` reicht den
  Issue-Text als Akzeptanzkriterium durch, `forge doctor` warnt bei
  ``judge.enabled`` ohne bindendes ``llm_judge_score``-Gate.

#### Tests

- **+13 Tests** (309 → 322): `test_judge.py` (+9: fail-closed, Mock-Modi,
  End-to-End rot→grün-KEEP via Judge-Gate + DISCARD bei Judge-fail),
  `test_spec.py` (+5: JudgeConfig, Gate-Kind, Warnung), `test_cli.py`
  (+1: doctor judge-check).

### Boundaries — was bewusst NICHT geändert wurde

- **Decide-Logik unangetastet**: `scoring.py`, `gates.py`,
  `keep_or_discard` — kein Diff. Der Judge füttert nur einen neuen
  Messwert ein; die Loop-Logik bleibt unberührt (Mantra 3).
- **EventKind-Set bleibt 18**, alle `payload_schema_version` bleiben
  ``"1.0"``. `eval_mode="judge"` war im Schema bereits vorgesehen.
- Capabilities ``merge_pr``/``push_to_main``/``push_force`` bleiben
  ``Literal[False]``. Self-Improvement-Verbot unangetastet — der Judge
  läuft nur gegen Target-Repos.

## [Unreleased] — v0.4: Board-driven Trigger Source

261 Tests grün, ruff clean.

### Hinzugefügt

#### `forge board-loop` — aktive Board-Trigger-Quelle

- **`forge board-loop`** — neuer CLI-Command in `forge-cli`. Pollt ein
  GitHub Project, filtert ready-Items (Status + Labels + Idempotenz),
  dispatched bis zu N Issues sequenziell durch die unveränderte 5-Phasen-
  Pipeline. Stoppt bei Backlog-leer, max-Limit oder hartem Run-Abort.
  Optionen: `--max N`, `--dry-run`, `--auto-merge`, `--issue 42 43`
  (override), `--multi-agent`, `--max-iterations`, `--max-turns`.
- **`BoardConfig`** in `forge-core/spec.py` — optionaler `board:`-Block
  in `project.yaml`. Felder: `provider`, `owner`, `project_number`,
  `filter_status`, `filter_labels`, `default_focus_template`,
  `default_template_id`. Strict-validated, additiv (kein Breaking
  Change auf bestehende Specs).
- **`forge_adapters.github.board`** — `list_ready_items`, `ReadyIssue`,
  `wrap_issue_body`, `BoardError`. Wrappt `gh project item-list` +
  `gh pr list`-Idempotenz-Check (kein Re-Dispatch bei offenem
  ``Closes #N``-PR). subprocess-DI für Tests.

#### Auto-Merge — Spec-konforme Grauzone

- **`queue_auto_merge`** in `forge_adapters.github.pr` — ruft
  `gh pr merge --auto --squash --delete-branch` auf, sodass GitHub
  **server-seitig** mergt sobald required Checks grün sind. forge
  selbst führt **keinen** synchronen `merge`-Subprozess aus — die
  ``merge_pr``-Capability bleibt typed ``Literal[False]``. Vor dem
  ersten Aufruf ``repo_supports_auto_merge``-Probe via
  ``gh repo view --json autoMergeAllowed`` mit klarer Remediation-
  Meldung wenn das Feature im Repo aus ist.
- **`forge run --auto-merge`** — neues Flag, durchgereicht zum PR-
  Erzeugungs-Pfad.
- **`forge board-loop --auto-merge`** — pro dispatched PR.

#### Refactoring

- `forge_cli.run.execute_run` — Body von `run_command` extrahiert
  als pure-Python-Funktion mit `RunOutcome`-Return. `board-loop`
  benutzt sie ohne Typer-Layer-Duplikation. Bestehende `forge run`-
  CLI-Semantik unverändert.
- `forge_cli.run.RunOutcome` — neues Dataclass kapselt
  ``RunResult`` + PR-URL/Number/Error + Auto-Merge-Status.

#### Tests

- **39 neue Tests** (222 → 261): `test_spec.py` +8 (BoardConfig),
  `test_board.py` +14 (Filter, Idempotenz, Error-Paths, wrap_issue_body),
  `test_pr.py` +7 (queue_auto_merge, repo_supports_auto_merge),
  `test_cli.py` +8 (board-loop dry-run, --auto-merge guards,
  remote-URL-Parser, "Backlog leer").

### Boundaries — was bewusst NICHT geändert wurde

- Die 5-Phasen-Pipeline (Propose → Mutate → Preflight → Eval → Decide)
  bleibt 1:1 unverändert. `board-loop` ist reine Orchestrations-Schicht
  darüber, nicht Loop-Logik.
- Capabilities (``merge_pr``, ``push_to_main``, ``push_force``) bleiben
  typed ``Literal[False]``.
- Score-Logic, Gates, Decision, alle 16 EventKinds: unangetastet.
- Self-Modification (Prinzip 3) bleibt verboten — `board-loop` fasst
  nichts in `forge/` an, nur in den Target-Repos.

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
