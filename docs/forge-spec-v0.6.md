# forge — Spezifikation v0.6

> Status: Working Draft v0.6
> Letzte Änderung: 2026-06-27
> Vorgänger: [`forge-spec-v0.5.md`](forge-spec-v0.5.md) — bleibt als
> historisches Dokument im Repo
> Diff-Doku: dieses Dokument beschreibt **nur** die Änderungen gegenüber
> v0.5. Alle Mantras, Prinzipien, Pipeline-Phasen, Kostenebenen,
> Capabilities-Defaults, Event-Felder, der Judge usw. aus v0.5/v0.4 gelten
> unverändert weiter, soweit hier nicht explizit etwas umformuliert wird.

> **Änderungen gegenüber v0.5** (zusammengefasst): v0.6 dokumentiert die
> seit v0.5 implementierte **Loop-2-Fabrik** (Conductor-Stage-Pipeline
> `requirements → … → done`), die **Session-Limit-Resilienz** (`forge run
> --resume`), zwei neue opt-in Roster-Glieder (**`/simplify`**-Skill und
> **reviewer**), den **`forge review-pr`**-Befehl (Agent reviewed offenen
> PR, opt-in Merge), das **Memory/Lessons-Learned**-Gedächtnis und den
> **`forge init`**-Befehl. Neue EventKinds: `RequirementsRefined`,
> `ReleaseTagged`, `LessonLearned` → **`len(EventKind) == 26`**.

---

## Teil 1 — Motivation

v0.5 schloss die Feature-Lücke der **einzelnen** Generation (Judge). v0.6
hebt forge von „ein Run pro Issue" auf eine **getaktete Fabrik**: ein
deterministischer Conductor (Loop 2) bewegt Issues durch eine Stage-Pipeline
und dispatcht pro Stage das passende Team. Dazu kommen drei Robustheits- und
Qualitäts-Bausteine (Resilienz gegen Session-Limits, Cleanup-/Review-Schritte
im Roster, ein Run-übergreifendes Gedächtnis) und zwei Bedienkomfort-Befehle
(`init`, `review-pr`).

Alle Änderungen respektieren **Mantra 3**: der Conductor entscheidet rein aus
dem Event-Strom, greift nie in Runner/Scoring/Gates ein; Memory und Lessons
sind read-only-Auswertung.

---

## Teil 2 — Was forge in v1 NICHT tut (unverändert)

Auto-Merge **durch forge selbst**, Population-Based Search, Bandit/BO,
Self-Improvement bleiben kategorisch ausgeschlossen. Präzisierungen in v0.6:

- **`forge review-pr` und `board-loop --auto-merge`** mergen nicht synchron
  durch forge: `review-pr` ist opt-in über `capabilities.merge_pr`, und
  `--auto-merge` **queued** GitHubs serverseitiges Auto-Merge (`gh pr merge
  --auto`). forge ruft nie `gh pr merge <N>` ohne `--auto`. Die
  `Literal[False]`-Capability bleibt damit gewahrt (siehe Teil 7).
- **`release`-Stage / `ReleaseTagged`** erzeugt höchstens Tag + GitHub-Release
  (kein Code-Change) und ist opt-in.

---

## Teil 3 — Architektur (Diff zu v0.5)

```
forge-core       ── + 3 EventKinds: RequirementsRefined, ReleaseTagged,
                      LessonLearned  (len(EventKind) == 26)
                    + additive payload-Bumps (1.0 → 1.1): PlanProposed
                      (subtasks, agents_used), RunFinished (decision
                      "rate_limited"), ProposalReceived (session_id),
                      ConductorTickCompleted (scheduled_resume_count)
forge-execute    ── + SubagentName += "simplify" (built-in Skill, kein Template)
                    + reviewer-Orchestrierungsschritt (opt-in)
                    + Session-Limit-Erkennung + Resume (CodingAgentRateLimited,
                      RunResumeScheduled, runner --resume-Pfad)
                    + worktrees.revert robust gegen gesperrtes .venv (-e .venv)
                    + tzdata als Pflicht-Dependency (reset_at-Parsing)
forge-cli        ── + Loop 2: Conductor-Pipeline requirements → … → done
                      (stages.py, conductor.py, heartbeat.py, schedule.py)
                    + forge board-loop --watch --conductor --max-parallel
                    + forge review-pr <N>
                    + forge init
                    + Memory/Lessons in forge analyze (read-only Views)
forge-adapters   ── + GitHub Project-Board (list_stage_items,
                      set_issue_stage_label), Review-Merge-Action-Template
```

Die Schicht-Boundaries aus v0.4/v0.5 gelten unverändert. Loop 2 lebt
**vollständig in `forge-cli`**, nie in `forge-execute`.

---

## Teil 4 — Event-Schema (3 neue Kinds)

`len(EventKind) == 26` (war 18 in v0.5-Zählung; +`ISSUE_TRIAGED` war bereits
v0.4; Loop-2 + Resilienz brachten `ConductorTickCompleted`,
`WorkItemStageChanged`, `WorkItemBlocked`, `RunResumeScheduled`; v0.6 ergänzt:)

- **`RequirementsRefined`** — ein requirements-Run hat ein rohes Issue zu
  testbaren Akzeptanzkriterien verdichtet. Advance-Signal `requirements→design`.
- **`ReleaseTagged`** — forge hat in der release-Stage einen Tag + GitHub-Release
  erzeugt. Advance-Signal `release→done`; opt-in `create_release`.
- **`LessonLearned`** — eine kuratierte, destillierte Lektion aus einem Run
  (Konventionen, Stolperfallen, Patterns). **Nicht** aus anderen Events
  ableitbar — Selbstauskunft des Master-Agenten. Dedupliziert nach Text.

**Additive Schema-Bumps** (alle `1.0 → 1.1`, alte Events lesen weiter, weil
neue Felder Defaults haben): `PlanProposed` (`subtasks`, `agents_used`),
`RunFinished` (Decision `rate_limited`), `ProposalReceived` (`session_id`),
`ConductorTickCompleted` (`scheduled_resume_count`). Keine Breaking Changes.

---

## Teil 5 — Subagent-Roster: `simplify` und `reviewer`

`SubagentName = Literal["architect", "developer", "tester", "simplify",
"reviewer", "operations"]`. `architect/developer/tester` sind Default-Roster;
`reviewer` und `simplify` sind **opt-in** (in `KNOWN_AGENTS`, **nicht** in
`DEFAULT_AGENTS`). `operations` bleibt reserviert (kein Template).

- **`reviewer`** (read-only): liest den kumulativen Diff kritisch gegen
  (Korrektheit, Surfaces/Forbidden, Security, Test-Qualität). Läuft als
  **letzter** Schritt; BLOCKING-Findings gehen zurück an den developer (zwei
  Runden max), dann re-verifiziert der tester. Er urteilt, entscheidet nicht,
  schreibt keinen Code — Abgrenzung zum gescorten, fail-closed **Judge**
  (Phase 4b, außerhalb der Orchestrierung).
- **`simplify`** ist **kein Task-Subagent**, sondern die built-in
  `/simplify`-Skill der Claude-Code-CLI (kein `.md`-Template). Der Master ruft
  sie über das **Skill**-Tool auf — die **eine** sanktionierte Ausnahme zum
  „Edit files yourself: NEVER" (Tool-Call, kein Hand-Editing). Position:
  `developer → tester(grün) → /simplify → tester(re-verify) → reviewer`.
  Freigeschaltet als gescopt `Skill(simplify)` in der Allowlist — nie das offene
  `Skill`. Bewusst opt-in (startet intern 4 parallele Review-Agenten →
  Kostentreiber). Kein Schema-Bump: `agents_used` darf `simplify` als
  „mitgewirkt" tragen.

Das aktive Roster steuert `build_orchestrator_prompt(roster)`: ohne `architect`
kein Plan-Schritt, ohne `tester` keine Verifikation, ohne `reviewer` kein
Review-Schritt, ohne `simplify` kein Skill-Schritt.

---

## Teil 6 — Loop 2: Conductor-Stage-Pipeline

Der Conductor (`forge board-loop --watch --conductor`) ist eine deterministische
State-Machine über GitHub-Stage-Labels. Pipeline:

```
forge:requirements → forge:design → forge:ready → forge:in-dev → forge:qa
   → forge:release → forge:done            (forge:blocked von überall erreichbar)
```

- **Advance ist rein** (`stages.advance(stage, signals)`), abgeleitet aus dem
  Event-Strom (`conductor.derive_signals`):
  | Stage | Advance-Signal | → Folge-Stage |
  |---|---|---|
  | requirements | `has_refined_spec` (`RequirementsRefined`) | design |
  | design | `has_plan` (`PlanProposed`) | ready |
  | in-dev | `has_open_pr` (`PRCreated`) | qa |
  | qa | `has_merged_pr` (`PRMerged`) | release |
  | release | `release_done` (`ReleaseTagged`) | done |
  `ready→in-dev` ist der reguläre Dev-Dispatch (kein In-Place-Signal).
- **`IN_PLACE_WORK_STAGES = {requirements, design, qa, release}`**: Stages, in
  denen ein Team *in-place* arbeitet (kein Stage-Wechsel beim Dispatch) und sein
  Advance-Signal selbst produziert.
- **Stage-spezifischer Dispatch:** `plan_tick` liefert `list[DispatchOrder]`
  (`number` + `stage`); `_run_conductor_watch` verzweigt nach `stage` (z.B.
  `design` → architect-Roster, `create_pr=False`, Output `PlanProposed`; sonst
  Dev-Loop mit PR). Mehrere Runs nebenläufig via `--max-parallel`.
- Jeder Tick ist ein `ConductorTickCompleted`-Event unter einer **Session-ULID**
  als `run_id`. Der Cron-Matcher (`schedule.py`) ist dependency-frei.

Mantra 3: der Heartbeat taktet `execute_run`, greift nie in Runner/Scoring/Gates
ein. Stage-Labels fallen mit den `on_issue_label`-Trigger-Keys zusammen — ein
Label, kein zweiter Konfig-Ort. Design + Phasenplan:
[`conductor-design.md`](conductor-design.md).

---

## Teil 7 — Session-Limit-Resilienz (`--resume`)

Läuft ein orchestrierter Run in ein Claude-Usage-/Session-Limit, ist das ein
**fortsetzbarer Zustand**, kein Fehlschlag. Drei strikt getrennte Schichten:

1. **Erkennen & sichern (Loop 1).** claude meldet das Limit irreführend als
   `result`-Event mit `subtype:"success"` ABER `is_error:true` +
   `api_error_status:429`. Erkennung in `claude_cli._is_rate_limited` (nicht über
   subtype/returncode). `propose()` wirft `CodingAgentRateLimited` (mit
   `session_id`, geparster `reset_at`, echtem `cost_usd`). Der Runner bucht den
   echten Cost, beendet als Decision **`rate_limited`**, sichert die
   Partial-Arbeit als `forge: WIP`-Commit (**kein** `revert`/`git clean`), und
   legt den Anker als **`RunResumeScheduled`** (original_run_id,
   resume_session_id, resume_at, resume_worktree, wip_commit, issue_number).
2. **Fortsetzen (Loop 1 + CLI).** `forge run --resume <run_id>` lädt den Anker,
   dockt via `worktrees.attach()` an den eingefrorenen Worktree an (Basis =
   WIP-HEAD) und ruft `propose(resume_session_id=…)` → `claude --resume`. Ein
   übergebener `run_id` IST das Resume-Signal. `resume_session_id` ist optionaler
   Protocol-Param — Agents ohne Resume ignorieren ihn (Plug-in-safe).
3. **Auto-warten (Loop 2).** `conductor.derive_pending_resumes(events, now)`
   leitet fällige Resumes rein aus dem Event-Strom ab (at-most-once).
   `resume_at == None` (Reset-Zeit unparsebar) = **nur manuell**.

`tzdata` ist Pflicht-Dependency von `forge-execute` (Windows hat keine IANA-DB;
ohne sie wäre `reset_at` immer `None`).

---

## Teil 8 — `forge review-pr`

Ein Agent reviewed einen **offenen** PR (read-only) und merged ihn **opt-in**:
nur bei approve + grünem CI + gesetzter `capabilities.merge_pr`. Emittiert
`PRReviewed` und (bei Merge) `PRMerged`. In der Conductor-QA-Stage verdrahtet
(`review_done`-Gate); eigenständig als CLI nutzbar. forge merged nicht synchron
selbst — der Merge läuft über `gh` mit denselben Capability-Schranken.

---

## Teil 9 — Memory / Lessons Learned (read-only)

`_project_memory.py` baut den Memory-Block für jeden Run-Prompt aus drei Quellen:
Operator-Seed (`.forge/memory.md`, von forge **nie** geschrieben), **Recent run
outcomes** (`RunStarted`+`RunFinished`+`PRMerged`, korreliert über `run_id`) und
jüngste Plan-Summaries. Bei Widerspruch gewinnen die Run-Outcomes über die
Seed-Prosa (der Seed veraltet zwangsläufig). `LessonLearned`-Events liefern
zusätzlich destillierte Lektionen; `forge analyze` rendert sie (Views
`lessons_learned` + Failure-/File-Hotspot-Recall). Alles read-only-Auswertung
des Event-Stroms (Mantra 3), keine neue Loop-Logik.

---

## Teil 10 — `forge init`

Legt ein **rudimentäres** `.forge/project.yaml` mit sicheren Defaults an
(`merge_pr: false`, `push_to_main: false`, `cost_caps`). **Idempotent**:
überschreibt eine bestehende Config nicht (die Operator-Datei bleibt erhalten).
Bewusst minimal — Surfaces/Gates/Eval-Suiten ergänzt der Operator (die
generierte Datei ist als Startpunkt gedacht, noch nicht zwingend
`doctor`-vollständig).

---

## Teil 11 — Was sich NICHT geändert hat

- 5-Phasen-Pipeline (Plan → Implement → Validate → Eval → Decide) und die
  Judge-Sub-Phase 4b bleiben. Loop 2 liegt **über** den Runs, nicht in ihnen.
- `scoring.py`, `gates.py`, `keep_or_discard` — **kein Diff**. Die rot→grün-
  Mechanik trägt weiterhin Bug-Fixes UND Features.
- Capabilities `merge_pr`/`push_to_main`/`push_force` bleiben `Literal[False]`;
  `review-pr` und `--auto-merge` mergen nie synchron durch forge.
- Self-Improvement-Verbot (Prinzip 3) unangetastet — forge ändert ihre eigene
  Config nie; der Conductor entscheidet nur aus dem Event-Strom.

---

## Anhang — Migration v0.5 → v0.6

Bestehende `.forge/project.yaml`-Specs sind **unverändert gültig**. Neue
opt-in Bausteine:

```yaml
# Roster um Cleanup + Review erweitern (kostet mehr, gründlicher)
triggers:
  on_issue_label:
    auto-feature:
      agents: [architect, developer, tester, simplify, reviewer]

# Fabrik aktivieren
board:
  provider: github
  owner: dein-user
  project_number: 4
  filter_status: "Todo"
```

Bestehende Events bleiben lesbar (kein Schema-Break). Die Größen-Invariante in
den Replay-/Schema-Tests ist jetzt `len(EventKind) == 26`.
