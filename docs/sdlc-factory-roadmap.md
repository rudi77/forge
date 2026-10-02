# forge als Software-Fabrik — Roadmap: kompletter SDLC bis Release

> Status: **Entwurf, nichts davon implementiert.** Dieses Dokument beschreibt
> die Lücken zwischen dem heutigen Conductor-Fließband und einer Fabrik, die
> ohne menschliches Nachschieben vom Wunsch bis zum Release läuft. Es endet mit
> den Entscheidungen, die der Operator treffen muss, bevor Code entsteht.
> Bezug: [`conductor-design.md`](conductor-design.md), [`forge-spec-v0.6.md`](forge-spec-v0.6.md).

## 0. Wo wir stehen

Das Fließband ist durchgängig verdrahtet (`stages.py`, `conductor.py`,
`board_loop.py::_run_conductor_watch`):

```
forge:requirements → forge:design → forge:ready → forge:in-dev → forge:qa
   → forge:release → forge:done            (forge:blocked von überall)
```

| Stage | Team | Advance-Signal | Status |
|---|---|---|---|
| requirements | Analyst-Run | `RequirementsRefined` | ✅ |
| design | architect | `PlanProposed` | ✅ |
| ready → in-dev | Dev-Loop | Dispatch, A1-Retry bis `MAX_DEV_RETRIES` | ✅ |
| in-dev → qa | — | `PRCreated` | ✅ |
| qa | Review-Merge-Agent | `PRMerged` | ✅ (merge opt-in) |
| release | deterministisch | `ReleaseTagged` | ✅ (ein Tag pro Issue) |

**Die Fabrik läuft also — aber sie bleibt an vier Stellen stehen oder braucht
einen Menschen, und sie wurde noch nie gegen ein echtes Board gefahren.**

| # | Lücke | Wo die Fabrik heute stoppt |
|---|---|---|
| L0 | Keine Live-Verifikation | gh-Kommandos nur gegen Stubs getestet |
| L1 | Kein Nacharbeits-Loop | `request_changes` → `review_done` sperrt QA, bis ein **Mensch** committet |
| L2 | Kein CI-Autofix im Conductor | roter CI → `decide_merge` merged nie → Item parkt in `qa` |
| L3 | Kein echtes Release | `forge-issue-<N>`-Tag pro Issue (`board_loop.py::_release_tag_for_issue`), keine Version, kein Changelog |
| L4 | Keine Intake/Zerlegung | jedes Issue muss ein Mensch anlegen; Schedule-Trigger haben keine Prompt-Quelle |

Zielbild nach L0–L4:

```
             ┌──────────── schedule (cron, focus) ─────────────┐
             ▼                                                  │
 forge:epic ──(Zerlegung, L4)──► n × forge:requirements → design → ready
                                                                   │
                       ┌──── review: request_changes (L1) ─────┐   ▼
                       │     ci: failed (L2)                   │ in-dev
                       ▼                                       │   │ PRCreated
                    in-dev ◄───────────────────────────────── qa ◄─┘
                                                               │ PRMerged
                                       release-train (L3) ◄────┘
                                              │ ReleaseTagged(version, items)
                                              ▼
                                         forge:done
```

---

## L0 — Live-Verifikation (Voraussetzung für alles)

**Problem.** `list_stage_items`, `set_issue_stage_label`, `create_release`,
`fetch_pr_head_committed_at` sind nur gegen gestubbte Subprozesse getestet
(`conductor-design.md` §11 Phase C). Jede weitere Stage baut auf diesen Kanten
auf — ein falsches gh-Flag frisst sonst die nächsten Wochen.

**Vorschlag.**
1. `forge doctor --board`: prüft, dass alle `forge:<stage>`-Labels im Repo
   existieren, und legt fehlende mit `--fix` an (`gh label create`). Heute ist
   das ein manueller Operator-Schritt.
2. Sandbox-Repo + ein Durchstich-Skript (`scripts/conductor-smoke.sh`): ein
   triviales Issue von `forge:design` bis `forge:done` mit
   `--max-parallel 1 --interval 60`, `merge_pr=true`, `create_release=true`.
3. Gefundene Abweichungen als Regression-Tests gegen die Stubs nachziehen.

**Kein** Schema-, Event- oder Stage-Change. Aufwand: klein.

---

## L1 — Nacharbeits-Loop: `qa → in-dev` bei `request_changes`

**Problem.** `_dispatch_review_run` emittiert `PRReviewed(verdict=request_changes)`.
Danach ist `review_done=True` und `plan_tick` überspringt das Item
(`conductor.py`, RESOLVE-Zweig `Stage.QA and review_done`). Wieder in Bewegung
kommt es nur, wenn neue Commits auf dem PR landen (Loop 1c,
`head_committed_at`) — und die liefert heute niemand außer einem Menschen. Der
Übergang `QA → IN_DEV` steht in `ALLOWED_TRANSITIONS`, wird aber nie ausgelöst.

**Design.**

*Signale* (`StageSignals`, rein aus Events + injiziertem `head_committed_at`):

- `changes_requested: bool` — das jüngste `PRReviewed` des offenen PRs hat
  `verdict=request_changes` **und** ist nicht veraltet (kein Commit danach).
- `rework_rounds: int` — Anzahl `PRReviewed(request_changes)` für die PRs des
  Items (überlebt Heartbeat-Restarts, wie `dev_attempts`).

*State-Machine* (`advance`):

- `qa` + `changes_requested` + `rework_rounds < MAX_REWORK_ROUNDS` → `in-dev`
  (reason `review_changes_requested`).
- `qa` + `changes_requested` + `rework_rounds >= MAX_REWORK_ROUNDS` → `blocked`
  (`WorkItemBlocked`, reason `rework_exhausted`). Default `MAX_REWORK_ROUNDS = 2`,
  analog `MAX_DEV_RETRIES`.
- `in-dev` → `qa` bei `has_open_pr` wird gegated: **nicht**, solange
  `changes_requested` noch gilt. Sobald der Rework-Run pusht, ist der Review
  veraltet (`head_committed_at > review.ts`), `changes_requested` fällt auf
  `False`, und der bestehende `has_open_pr`-Pfad schreibt `in-dev → qa` fort →
  frisches Review. **Der Loop-1c-Mechanismus wird damit wiederverwendet, nicht
  dupliziert.**

*Dispatch* (`plan_tick` RESOLVE): `in-dev` + `has_open_pr` + `changes_requested`
→ `DispatchOrder(stage=IN_DEV, kind="rework")`. `DispatchOrder` bekommt ein
optionales `kind`-Feld (`"dev" | "rework" | "ci_fix"`), damit die Wiring-Schicht
verzweigen kann, ohne die Stage-Semantik zu überladen.

*Run auf bestehendem PR-Branch* — **das neue Primitiv, das L1 und L2 teilen:**

- `WorktreeManager.create_on_branch(run_id, branch)`: Worktree auf
  `origin/<branch>` statt auf neuem `forge/<run_id>`. `base_commit` = PR-Head.
- Ergebnis wird auf **denselben** Branch gepusht (fast-forward, nie `--force`),
  kein neuer PR. Leitplanke: nur Branches mit Präfix `forge/` (forge rührt nie
  einen menschlichen Branch an); `push_to_main`/`push_force` bleiben hart-deny.
- `execute_run(..., create_pr=False, push_to_branch=<branch>)` — der Runner
  bleibt unverändert, nur Worktree-Erzeugung und Effekt danach unterscheiden
  sich (wie beim Resume-Pfad via `attach()`).

*Kontext für den Dev-Run:* Issue-Text + Review-Begründung. `PRReviewed` trägt
die Begründung heute **nicht** im Event (nur auf GitHub via `post_pr_review`).
Für Replay-Fähigkeit (Mantra 2): `PRReviewedPayload` **1.0 → 1.1** additiv um
`reasoning_blob: str | None` (CAS-Ref). Die Begründung wird als UNTRUSTED
gewrappt in den Prompt gelegt (sie ist LLM-Output über fremden Code).

*Events:* kein neuer Kind. `RunStarted.trigger` bekommt additiv `"rework"`
(`TriggerKind` ist ein `Literal` → Schema-Bump `RunStarted` 1.0 → 1.1).

*Mantra-Check:* Die Runden zählt der Conductor (Loop 2) über `PRReviewed`-Events
— **nicht** der Runner. Die interne „zwei Runden max"-Prompt-Regel des
reviewer-Subagents bleibt davon unberührt.

*Tests:* `advance`-Tabelle (qa→in-dev, qa→blocked, in-dev-Gate), `derive_signals`
(veraltet vs. aktuell), `plan_tick` (rework-Order, Kapazität), Worktree auf
bestehendem Branch (echtes git, temp-Repo), Präfix-Guard.

---

## L2 — CI-Autofix im Conductor

**Problem.** `on_ci_failure` existiert in der Spec (`CIFailureTriggerConfig`,
default Roster `["developer"]`) und als GitHub-Action-Template
(`forge-ci-autofix.yml`), ist aber nicht im Conductor angeschlossen. Ein PR mit
rotem CI wird vom QA-Agent reviewt (Kosten!), `decide_merge` blockt mit
`ci_not_green`, und das Item parkt in `qa`.

**Design** (baut auf dem L1-Primitiv auf):

- *Signal:* `ci_status: Literal["pass","fail","pending","none"]`, von der
  Wiring-Schicht via `summarize_ci` injiziert — genau wie `head_committed_at`
  (CI-Zustand steht nicht im Event-Strom; `derive_signals` bleibt rein).
  `ci_fix_attempts: int` aus `RunStarted(trigger="ci_failure")` für das Issue.
- *State-Machine:* `qa` + `ci_status=="fail"` → `in-dev` (reason `ci_failed`),
  bzw. `blocked` nach `MAX_CI_FIX_ATTEMPTS` (Default 2).
- *QA-Gate:* QA-Review wird nur dispatcht, wenn `ci_status in {"pass","none"}` —
  `pending` wartet, `fail` geht in den Fix-Pfad. Spart das teure Review auf
  einem PR, der ohnehin nicht gemergt werden kann.
- *Dispatch:* `DispatchOrder(kind="ci_fix")` → Run auf PR-Branch (L1-Primitiv),
  Roster aus `triggers.on_ci_failure.agents`. Kontext: Namen der roten Checks +
  `gh run view --log-failed`, **auf N KB gekürzt und als UNTRUSTED gewrappt**
  (CI-Logs können Prompt-Injection aus Testdaten enthalten).
- *Events:* kein neuer Kind (`trigger="ci_failure"` existiert bereits).

**Abgrenzung:** „Flake" wird nicht erkannt oder still re-runnt. Ein roter Check
ist ein Fix-Auftrag; nach `MAX_CI_FIX_ATTEMPTS` eskaliert ein Mensch.

---

## L3 — Echtes Release: Release-Train statt Tag pro Issue

**Problem.** `_dispatch_release_run` taggt `forge-issue-<N>` pro Issue. Das ist
ein Marker, kein Release: keine Version, kein Changelog, keine Bündelung. Die
Spec hat die Felder schon (`ReleaseConfig.conventional_commits`, `changelog`),
sie werden aber nicht genutzt.

**Zentrale Randbedingung:** `push_to_main` ist hart `False`. Eine
`CHANGELOG.md`-/Versions-Änderung auf `main` darf forge also **nicht direkt
committen**. Daraus folgt das Release-PR-Muster (à la release-please):

```
 n Items in forge:release ──► Release-Run: Version + Changelog berechnen
                              → PR "chore(release): vX.Y.Z" (Branch forge/release-…)
                              → durchläuft den normalen QA-/Merge-Pfad
 PRMerged(release-PR)    ──► Tag vX.Y.Z + gh release (Notes = Changelog-Abschnitt)
                              → ReleaseTagged(version, issue_numbers=[…])
                              → alle enthaltenen Items: release → done
```

**Design.**

- *Versionierung (rein, deterministisch, voll unit-testbar):*
  `next_version(last: SemVer, commits: list[str]) -> SemVer` — `feat:` → minor,
  `fix:`/`perf:` → patch, `!`/`BREAKING CHANGE:` → major. Bei `0.x` hebt ein
  Breaking Change die Minor-Version (SemVer-Konvention vor 1.0).
- *Changelog (rein):* `render_changelog(version, commits, prs)` gruppiert nach
  Typ, verlinkt PRs und Issues. Optional kann ein Release-Notes-Agent die Prosa
  glätten — **fail-open auf den deterministischen Text** (Notes sind
  Information, kein Gate).
- *Auslöser:* `ReleaseConfig` additiv:
  - `mode: Literal["per_issue","train"] = "per_issue"` (Default = heutiges
    Verhalten, rückwärtskompatibel),
  - `schedule: str | None` (Cron, nutzt `schedule.py`),
  - `min_items: int = 1`,
  - `version_files: list[str]` (z.B. `pyproject.toml`), die der Release-PR bumpt.
- *Events:* `ReleaseTaggedPayload` **1.0 → 1.1** additiv: `version: str | None`,
  `issue_numbers: list[int]` (Default `[]`). `derive_signals.release_done`
  prüft `issue_number == N or N in issue_numbers`. Der Release-PR selbst ist
  ein normales `PRCreated` mit Label `forge:release-pr` — **kein** neuer Kind.
- *Capability:* bleibt `create_release` (opt-in). Merge des Release-PRs folgt
  derselben `merge_pr`-Mehrfachbedingung wie jeder andere PR.
- *Code-Ort:* `forge-cli/release.py` (Train-Planung, Loop 2), reine
  Versions-/Changelog-Logik ebenfalls dort; `create_release` bleibt im Adapter.
- *Fabrik-KPI:* `factory_releases`-View (Release-Frequenz, Items pro Release,
  Lead-Time Issue-Eröffnung → Release) — reine Auswertung, kein Loop-Eingriff.

**Bewusst nicht in L3:** Deployment. forge kennt keine Zielumgebung.
Siehe §5.

---

## L4 — Intake: Epic-Zerlegung und Schedule-Quellen

**Problem.** Die Fabrik verarbeitet nur Issues, die ein Mensch fertig angelegt
hat. Ein großer Wunsch („Mehrbenutzer-Support") muss von Hand in Issues mit
`Depends-On:` zerlegt werden. Außerdem liegt die Cron-Maschinerie
(`schedule.py`) seit Phase B ungenutzt, weil ein Schedule-Trigger „keine
Prompt-Quelle" hat (`conductor-design.md` §11).

**Design.** Eine Arbeit *erzeugende* Stage vor `requirements`:

- *Stage:* `forge:epic` (neu in `Stage`, `IN_PLACE_WORK_STAGES`). Advance:
  `epic` + `has_decomposition` → `forge:tracking` (neuer Nicht-Pipeline-Zustand),
  `tracking` → `done`, sobald alle Kind-Issues `done` sind (aus dem Board
  ableitbar, wie die Dependency-Auflösung).
- *Run:* Planner-Run (architect-Roster, `create_pr=False`) liefert einen
  `---FORGE-SUBISSUES-BEGIN/END---`-Block: Titel, Body mit
  Akzeptanzkriterien und Abhängigkeiten zwischen den Kind-Issues.
  Parser best-effort wie `_plan_parser`; kein Block → kein Effekt.
- *Effekt:* forge legt die Kind-Issues per `gh issue create` an (Label
  `forge:requirements`, `Depends-On:` mit den realen Nummern aufgelöst,
  Verlinkung als GitHub-Sub-Issue, wenn verfügbar).
  **Neue Capability `create_issues: bool = False`** (opt-in) plus
  `max_issues_per_epic` (Default 8) — ungebremste Issue-Erzeugung ist der
  neue Kostentreiber.
- *Events:* neuer Kind **`EpicDecomposed`** (26 → 27): `epic_number`,
  `child_numbers`, `dependency_edges`. Nicht aus anderen Events ableitbar →
  eigenes Event ist gerechtfertigt (gleiche Begründung wie `LessonLearned`).
- *Schedule als Quelle:* ein Schedule-Trigger erzeugt beim Feuern ein
  `forge:epic`-Issue mit seinem `focus` als Body (z.B. „nächtlicher
  Tech-Debt-Sweep"). Damit hat der Schedule seine Prompt-Quelle, und die
  Arbeit läuft durch dieselbe Zerlegung und Triage wie alles andere — kein
  zweiter Pfad. Triage (fail-open) schließt Duplikate gegen offene Issues.
- *Sicherheit:* Epic-Body und LLM-Titel sind untrusted; Issue-Erzeugung nur
  über argv (nie `shell=True`). Sub-Issues erben **nie** Labels, die Stages
  überspringen (Kinder starten immer in `requirements`).
- *Mantra 1:* Zerlegungsqualität wird messbar über den Anteil der Kinder, die
  `done` erreichen, vs. `blocked`/geschlossen (`factory_epics`-View).

---

## 5. Bewusst später (nicht in L0–L4)

| Thema | Warum später |
|---|---|
| **Deploy / Post-Release-Verifikation** | Braucht ein Ziel-Umgebungsmodell (`deploy:`-Command in der Spec, trusted, + Smoke-Eval). Incident → Issue in `forge:requirements` würde den Kreis schließen. Erst sinnvoll, wenn L3 echte Versionen liefert. |
| **Merge-Queue / Integrations-Run** (Phase D) | Erst bei `--max-parallel > 1` mit realen Konflikten nötig; L1/L2 liefern die Daten, wie oft das passiert. |
| **Per-Rolle-Telemetrie** (`cost_per_role`) | Rohdaten liegen in `.forge/logs/*.jsonl`; Aggregation ist eigene Event-Logik. |
| **PBS / Bandit** (Spec v2/v3) | gegated auf ≥100 bzw. ≥300 Runs. |

---

## 6. Empfohlene Reihenfolge

1. **L0** — sonst baut alles auf unverifizierten gh-Kanten.
2. **L1 inkl. Branch-Primitiv** — schließt den häufigsten Stillstand; das
   Primitiv „Run auf bestehendem forge-PR-Branch" ist die Basis für L2.
3. **L2** — billig auf L1; spart zusätzlich QA-Reviews auf rotem CI.
4. **L3** — macht das Ende real (Versionen, Changelog).
5. **L4** — öffnet die Front (Epics, Schedules); die meiste neue Angriffsfläche
   (Issue-Erzeugung), deshalb zuletzt und opt-in.

Schema-Bilanz über alles: **+1 EventKind** (`EpicDecomposed`, 27), additive Bumps
`PRReviewed` 1.1, `RunStarted` 1.1, `ReleaseTagged` 1.1. Keine Breaking Changes.
`scoring.py`/`gates.py`/Runner-Decide bleiben unangetastet (Mantra 3) — alles
Neue lebt in Loop 2 (`forge-cli`), im Adapter und in additiven Spec-Feldern.

---

## 7. Entscheidungen für den Operator

| # | Frage | Vorschlag |
|---|---|---|
| E1 | L1: Soll das Item bei Nacharbeit sichtbar auf `forge:in-dev` zurückspringen oder in `qa` bleiben und dort in-place nachgearbeitet werden? | **Zurück nach `in-dev`** — der Board-Zustand zeigt, wer gerade arbeitet; der Übergang ist schon erlaubt. |
| E2 | L1/L2: Darf forge auf einen bestehenden PR-Branch pushen (nur `forge/*`, nur fast-forward)? | **Ja**, mit Präfix-Guard; menschliche Branches nie. |
| E3 | L1/L2: Rundenlimits `MAX_REWORK_ROUNDS` / `MAX_CI_FIX_ATTEMPTS` als Konstante oder Spec-Feld? | Erst **Konstante = 2** (wie `MAX_DEV_RETRIES`), Spec-Feld, wenn Daten es rechtfertigen. |
| E4 | L3: Release-PR-Muster (Changelog im Repo, via PR) oder nur Tag + Release-Notes ohne Repo-Änderung? | **Release-PR** — einzig mit `push_to_main=false` verträglicher Weg zu einem versionierten Changelog im Repo. |
| E5 | L3: Tag-Schema `vX.Y.Z` und der heutige `forge-issue-<N>` daneben weiter? | `mode: train` → nur `vX.Y.Z`; `per_issue` bleibt Default für bestehende Setups. |
| E6 | L4: Neue Capability `create_issues` — einverstanden, dass forge selbst Issues anlegt? | **Ja, opt-in + Obergrenze pro Epic.** |
| E7 | L4: Kind-Issues starten in `requirements` oder direkt in `design`, wenn der Planner bereits Akzeptanzkriterien liefert? | **`requirements`** — eine Stage mehr, dafür ein einheitlicher Qualitätsfilter. |
| E8 | Deploy (§5) überhaupt im forge-Scope, oder endet die Fabrik bewusst beim Release? | Endet beim Release, bis L3 läuft; danach neu bewerten. |
