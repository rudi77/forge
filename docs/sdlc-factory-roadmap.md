# forge als Software-Fabrik — Roadmap: kompletter SDLC bis Release

> Status: **umgesetzt** (Stand 2026-10-02, Branch `ccr-3e5ca8c7-cr78gv`). Alle
> Abschnitte L0–L3, P, A und G sind implementiert und getestet; die
> Entscheidungen E1–E14 wurden wie vorgeschlagen getroffen. Spec-Diff:
> [`forge-spec-v0.7.md`](forge-spec-v0.7.md). Was bewusst vom Entwurf
> abweicht und was noch offen ist, steht in **§8 Umsetzungsstand** am Ende.
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

**Die Fabrik läuft also, aber sie bleibt an mehreren Stellen stehen oder
braucht einen Menschen, sie ist fest an GitHub gebunden, und sie wurde noch nie
gegen ein echtes Board gefahren.**

| # | Lücke | Wo die Fabrik heute stoppt |
|---|---|---|
| L0 | Keine Live-Verifikation | gh-Kommandos nur gegen Stubs getestet |
| **P** | **Nur GitHub** | `BoardConfig.provider: Literal["github"]`; `board_loop`/`review_pr`/`run` rufen `forge_adapters.github.*` direkt auf |
| L1 | Kein Nacharbeits-Loop | `request_changes` → `review_done` sperrt QA, bis ein **Mensch** committet |
| L2 | Kein CI-Autofix im Conductor | roter CI → `decide_merge` merged nie → Item parkt in `qa` |
| **A** | **forge erzeugt keine Arbeit** | Specs bleiben im Event-Store; Issues, Bugs und Folge-Aufgaben legt nur ein Mensch an (ersetzt das frühere L4) |
| **G** | **Kein Arbeitsgraph über Worktrees** | Parallelität nur über unabhängige Issues (`--max-parallel`) oder Subagents im **selben** Worktree |
| L3 | Kein echtes Release | `forge-issue-<N>`-Tag pro Issue (`board_loop.py::_release_tag_for_issue`), keine Version, kein Changelog |

Zielbild:

```
  Wunsch / schedule / CI-rot auf main / Findings aus Runs   (A: Arbeit erzeugen)
             │
             ▼
 forge:epic ──(Spec + Zerlegung, A)──► Arbeitsgraph (G): n × requirements → design → ready
                                        Knoten parallel in eigenen Worktrees ─┘
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

  Alles oben läuft gegen ein Provider-Interface (P): GitHub | Azure DevOps | …
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

## P — Provider-Unabhängigkeit: GitHub, Azure DevOps, später weitere

**Problem.** GitHub ist nicht hinter einer Schnittstelle, sondern im ganzen
CLI-Code verteilt:

- `BoardConfig.provider: Literal["github"]` (`spec.py`), nur `forge_adapters/github/`.
- `board_loop.py`, `review_pr.py`, `run.py` importieren und rufen
  gh-Funktionen direkt (`create_pr_for_run`, `post_pr_review`, `merge_pr`,
  `fetch_pr_*`, `list_stage_items`, `set_issue_stage_label`, `create_release`, …).
- **Bestehender Grenzverstoß:** `forge-execute/triage/{base,llm}.py`
  importieren `ReadyIssue` aus `forge_adapters.github.board`, und
  `triage/gh.py` ruft selbst `gh issue comment/close`. Laut CLAUDE.md darf
  `forge-execute` nichts aus `forge-adapters` importieren.
- GitHub-Begriffe stecken in Namen (`issue_number`, `pr_number`, „Label").

Jeder weitere Schritt (L1–L3, A, G) fügt neue gh-Aufrufe hinzu. **Deshalb kommt
die Abstraktion vor diesen Schritten**, sonst wird alles doppelt gebaut.

**Design — zwei getrennte Schnittstellen**, weil Tracker und Code-Host in der
Praxis auseinanderfallen (z.B. Azure Boards + GitHub-Repos):

```
forge-core   : neutrale Datentypen  WorkItem, WorkItemRef, ChangeRequest,
               CiStatus, ReviewVerdict  (ersetzen ReadyIssue in triage/)
forge-adapters/base.py : Protocols
   WorkTracker  – list_items(stage…), get_item, set_stage, comment, close,
                  create_item(kind, title, body, parent, depends_on), link
   CodeHost     – push_branch, open_change(…), review(…), merge(…),
                  ci_status(…), head_committed_at(…), fetch_diff(…),
                  create_release(tag, notes)
forge-adapters/github/  : GitHubTracker + GitHubCodeHost   (heutiger Code, verschoben)
forge-adapters/azure/   : AzureBoardsTracker + AzureReposCodeHost
forge-adapters/registry : provider-Name → Implementierung
```

`forge-cli` bekommt die Implementierungen per Konstruktor bzw. Kontext
(`ForgeContext.tracker`, `ForgeContext.code_host`) und kennt danach keinen
Anbieter mehr. Die Conductor-Logik (`stages`, `conductor`, `dependencies`)
ist schon rein und bleibt unverändert.

**Abbildung Azure DevOps:**

| forge-Begriff | GitHub | Azure DevOps |
|---|---|---|
| Work-Item | Issue | Work Item (Epic/Feature/User Story/Bug/Task) |
| Stage | Label `forge:<stage>` | Tag `forge:<stage>` (Vorschlag, s. E9) |
| Abhängigkeit | `Depends-On: #N` im Body | `Depends-On:` **und** native Predecessor-Links |
| Eltern/Kind | Sub-Issue | Parent/Child-Link |
| Change-Request | Pull Request | Pull Request (Azure Repos) |
| Review approve / request changes | `gh pr review` | Reviewer-Vote +10 / −5 („waiting for author") |
| CI-Status | Check-Runs (`statusCheckRollup`) | PR-Policies / Build-Validation |
| Merge | `gh pr merge` | PR completion (squash) |
| Release | Tag + GitHub Release | Tag (+ optional Pipeline-Artefakt); kein „Release"-Objekt im Repo |
| Webhook | GitHub-Webhook / Action | Service Hook |
| Action-Templates | `.github/workflows/*.yml` | `azure-pipelines/*.yml` |

IDs sind bei beiden Anbietern ganze Zahlen → `issue_number`/`pr_number` in
den Events bleiben gültig, **kein Breaking Change**. Additiv bekommt
`RunStarted` ein `provider: str | None` (Bump zusammen mit L1 auf 1.1), damit
ein Event-Strom mit mehreren Anbietern eindeutig bleibt.

**Spec:** `BoardConfig.provider` wird zu `Literal["github","azure_devops"]`,
dazu optional `code_host:`-Block (Default = gleicher Anbieter). Azure braucht
`organization`, `project`, `repository`; Auth über `AZURE_DEVOPS_EXT_PAT`
(nie in der Spec).

**Zugriffsweg:** gh-Muster beibehalten (Subprozess, argv, gestubbt testbar) →
`az boards` / `az repos` (azure-devops-Extension). Wo die CLI Lücken hat
(Policy-Evaluations, Tags setzen ohne Überschreiben), REST über einen kleinen
Client. Entscheidung E10.

**Tests:** Eine **gemeinsame Vertrags-Testsuite** pro Protocol, die gegen jede
Implementierung (mit gestubbtem Subprozess) läuft. Ein neuer Anbieter ist
fertig, wenn er dieselbe Suite besteht. Plus ein In-Memory-`FakeTracker`/
`FakeCodeHost` für Conductor-Tests ohne Subprozess.

**Schritte:**
- **P0** (reines Refactoring, keine Verhaltensänderung): Protocols +
  neutrale Typen, GitHub-Code dahinter, Grenzverstoß in `triage/` beheben.
  Erfolgskriterium: alle bestehenden Tests grün, `forge-cli` importiert
  nichts mehr aus `forge_adapters.github` außer der Registry.
- **P1**: Azure-DevOps-Adapter + Pipelines-Templates + `forge doctor`
  prüft `az`-Login und Tags.
- Weitere Anbieter (GitLab, Gitea, Jira als reiner Tracker) = neues
  Adapter-Paket, das die Vertrags-Suite besteht.

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

## A — forge erzeugt Arbeit: Specs, Epics, Issues, Bugs

> Ersetzt das frühere L4 (Epic-Zerlegung) und erweitert es um Specs und
> Bugs/Folge-Aufgaben aus allen Phasen.

**Problem.**

- Die requirements-Stage erzeugt ein `RequirementsRefined`-Event, aber die
  verfeinerten Akzeptanzkriterien landen **nirgends, wo Menschen oder die
  nächste Stage sie lesen**: kein Kommentar am Issue, keine Datei im Repo. Der
  design-Run arbeitet wieder auf dem Original-Issue-Text.
- Jedes Work-Item legt ein Mensch an. Ein großer Wunsch muss von Hand in
  Issues mit `Depends-On:` zerlegt werden.
- Was Agents unterwegs finden (Tester sieht einen fremden Bug, Reviewer
  markiert „außerhalb des Scopes", CI auf `main` wird rot), geht verloren.
- Schedule-Trigger haben keine Prompt-Quelle (`conductor-design.md` §11).

### A1 — Specs als versionierte Artefakte

- **Feature-Spec als Datei im Repo:** `docs/specs/<id>-<slug>.md` mit Ziel,
  Nicht-Zielen, Akzeptanzkriterien (testbar, nummeriert), offenen Fragen,
  betroffenen Bereichen. Sie kommt über einen **Spec-PR** ins Repo (Label
  `forge:spec`), durchläuft den normalen Review-/Merge-Pfad und ist damit
  versioniert, diffbar und von Menschen kommentierbar. Direkt auf `main`
  schreiben geht ohnehin nicht (`push_to_main=false`).
- **Kleine Items** (Bug, Task) bekommen statt einer Datei einen strukturierten
  Kommentar `## forge: Akzeptanzkriterien` am Work-Item (über `WorkTracker.comment`).
- Die design- und dev-Runs lesen die Spec (Datei bzw. Kommentar) als
  Akzeptanzkriterium; der Judge prüft gegen genau diese Kriterien.
- *Events:* `RequirementsRefinedPayload` 1.0 → 1.1 additiv um
  `spec_path: str | None`, `spec_blob: str | None` (CAS-Ref für Replay).
- *Stage-Advance:* `requirements → design` erst, wenn die Spec gemergt
  (Feature) bzw. der Kommentar geschrieben ist (Bug/Task). Mit
  `requires_human_review: true` am Trigger ist der Spec-PR das natürliche
  menschliche Gate: forge merged ihn dann nicht selbst.

### A2 — Work-Items erzeugen: Quellen

| Quelle | Wann | Ergebnis |
|---|---|---|
| **Epic-Zerlegung** | Item in `forge:epic` | Kind-Items (Feature/Story/Task) mit Abhängigkeiten → Arbeitsgraph (G) |
| **Run-Findings** | Tester/Reviewer findet etwas außerhalb des Auftrags | Bug/Task mit Fundstelle, Kontext-Run verlinkt |
| **Review-Folgeaufgaben** | QA-Review markiert ein Finding als „nicht blockierend, später" | Task, verlinkt mit dem PR |
| **CI rot auf `main`** | `ci_status=fail` auf dem Default-Branch | Bug mit Check-Namen und gekürztem Log |
| **Schedule** | Cron mit `focus` (z.B. Tech-Debt, Dependency-Updates) | ein `forge:epic`-Item mit dem Fokus als Body → normale Zerlegung |
| **(später) Post-Release** | Smoke-Check nach Release scheitert | Bug, verlinkt mit dem Release |

Agents melden Funde über einen optionalen Block
`---FORGE-WORKITEMS-BEGIN/END---` (Typ, Titel, Body, Abhängigkeiten,
erwartete Dateien), geparst best-effort wie `_plan_parser`/`_lesson_parser`:
kein Block, keine Items; forge erfindet nie welche. Angelegt wird über
`WorkTracker.create_item` (P), also bei GitHub und Azure DevOps gleich.

**Neutrale Typen:** `WorkItemKind = epic | feature | story | bug | task | spec`.
GitHub: Labels (`type:bug` …) bzw. Issue-Types, wo verfügbar. Azure DevOps:
der passende Work-Item-Typ des Prozess-Templates (Mapping in der Spec
konfigurierbar, weil Agile/Scrum/CMMI unterschiedliche Typen haben).

### A3 — Leitplanken (die Fabrik darf sich nicht selbst zuschütten)

Eine Maschine, die ihre eigene Arbeit erzeugt, ist die teuerste mögliche
Endlosschleife. Deshalb:

- **Capability `create_work_items: bool = False`** (opt-in), dazu Obergrenzen
  `max_items_per_epic` (Default 8) und `max_items_per_day` (Default 20).
- **Menschliches Gate per Default:** erzeugte Items starten in einer neuen
  Stage **`forge:proposed`**, die der Conductor **nie** selbst verlässt. Erst
  wenn ein Mensch auf `forge:requirements` umstellt, arbeitet die Fabrik daran.
  Mit `auto_accept: [bug, task]` kann der Operator das gezielt pro Typ
  abschalten. Kinder eines vom Menschen freigegebenen Epics gelten als
  freigegeben.
- **Deduplizierung, deterministisch:** jedes erzeugte Item trägt
  `forge-fingerprint: sha256:<…>` im Body (Hash über Quelle + normalisierter
  Fundstelle). Existiert der Fingerprint schon, wird nur kommentiert, nicht
  neu angelegt. Zusätzlich prüft die bestehende Triage (fail-open) auf
  inhaltliche Duplikate.
- **Untrusted Input:** Epic-Bodies, CI-Logs und LLM-Titel nur über argv,
  nie `shell=True`; in Prompts als UNTRUSTED gewrappt. Erzeugte Items können
  keine Stage überspringen.
- **Herkunft sichtbar:** Label/Tag `forge:generated` + Link auf den Run.

### A4 — Events

Ein neuer, generischer Kind **`WorkItemCreated`** (26 → 27): `number`,
`kind`, `source` (`epic_decomposition | run_finding | review_followup |
ci_main_red | schedule | post_release`), `parent: int | None`,
`depends_on: list[int]`, `expected_files: list[str]`, `fingerprint`,
`origin_run_id`. Daraus sind Eltern/Kind-Beziehung und Abhängigkeitskanten
rekonstruierbar — das früher geplante `EpicDecomposed` entfällt.

*Mantra 1:* Messbar über einen `factory_intake`-View: Anteil erzeugter Items,
die `done` erreichen, vs. vom Menschen geschlossen/abgelehnt, pro `source`.
Eine Quelle mit niedriger Annahmequote ist ein Signal, sie abzuschalten.

**Code-Ort:** Parser + Leitplanken-Logik (rein) in `forge-cli`
(`intake.py`), Anlage über `WorkTracker` im Adapter. Der Runner meldet nur den
geparsten Block weiter; er legt nie selbst Items an (Mantra 3).

---

## G — Arbeitsgraph über Worktrees: zerlegen und parallelisieren

**Was es schon gibt.**

- Jeder Run hat einen eigenen Worktree (`.forge/worktrees/<run_id>`, Branch
  `forge/<run_id>`), und `worktrees.py` serialisiert nur `git worktree add/remove`.
- Der Conductor kann mit `--max-parallel N` mehrere **unabhängige** Items
  gleichzeitig in eigenen Worktrees bearbeiten (ThreadPool, ein geteilter,
  thread-sicherer `EventStore`).
- Abhängigkeiten zwischen Items über `Depends-On:` + topologische Reihenfolge
  + Zykluserkennung (`dependencies.py`).
- Innerhalb eines Runs parallelisiert der Master-Agent unabhängige Subtasks,
  aber alle Subagents teilen **einen** Worktree (nur per Prompt abgesichert).

**Was fehlt.**

1. Niemand **baut** den Graphen: Kanten schreibt heute ein Mensch.
2. Parallelität kennt keine **Dateikonflikte**: zwei unabhängige Items, die
   dieselben Dateien ändern, laufen gleichzeitig und kollidieren beim Merge.
3. Nach einem Merge wird der Geschwister-PR nicht auf den neuen Stand gebracht.
4. Für große Items gibt es keinen Weg „viele kleine Worktrees, ein Ergebnis".

### Design

**Ebene 1 — der Graph lebt auf Work-Item-Ebene, nicht im Runner.** Ein Knoten
ist ein Work-Item mit eigenem Run, eigenem Worktree und eigenem PR. Den Graphen
erzeugt die Zerlegung aus A (Kinder + `depends_on` + `expected_files`), der
Conductor plant ihn. So bleibt der Runner unverändert (Mantra 3), jeder Knoten
ist einzeln messbar, replay-fähig und reviewbar, und es funktioniert bei
jedem Anbieter gleich.

**Scheduling-Regeln** (rein, in `conductor.plan_tick`/`dependencies.py`):

- Ein Knoten ist bereit, wenn alle Vorgänger `done` sind (gibt es schon).
- **Konfliktkanten:** Zwei bereite Knoten, deren `expected_files` sich
  überschneiden (Glob-Match über `GitIgnoreSpec`), laufen **nicht**
  gleichzeitig. Der Planner leitet daraus eine implizite Kante nach
  Issue-Nummer ab — deterministisch, nicht als Event gespeichert, sondern
  aus den Daten jedes Ticks neu berechnet.
  `expected_files` steht als `Touches: src/auth/**, tests/auth/**` im Body,
  parallel zu `Depends-On:`. Der Operator kann es also im Tracker korrigieren.
- **„Wenn möglich":** parallel nur, was keine Kante und keinen Konflikt hat.
  **„Wenn nötig":** Kapazität = `min(max_parallel, Budget-Rest / ⌀Kosten pro Run)`.
  Bei kleinem Backlog oder knappem Budget läuft die Fabrik sequenziell.
  Die ⌀-Kosten kommen aus den bestehenden Fabrik-Views (Mantra 1).
- Ohne `Touches:` gilt ein Knoten als „berührt alles" und läuft allein
  (sicherer Default).

**Ebene 2 — Integrations-Branch für große Items (optional pro Epic).** Statt
n PRs gegen `main`:

```
main ──► forge/epic-42  (Integrations-Branch, vom Conductor angelegt)
           ├── forge/<run_a>  PR → forge/epic-42   (Knoten A, eigener Worktree)
           ├── forge/<run_b>  PR → forge/epic-42   (Knoten B, parallel zu A)
           └── forge/<run_c>  PR → forge/epic-42   (Knoten C, nach A)
         forge/epic-42 ──► ein PR gegen main, wenn alle Kinder gemergt sind
```

Kinder werden gegen den Integrations-Branch gemergt (nicht `main` → mit
`merge_pr` verträglich, weil das Ziel ein `forge/*`-Branch ist); der finale PR
gegen `main` geht durch die normale QA. Wird per Epic-Feld
`integration: branch | direct` gewählt; Default `direct`.

**Nach jedem Merge — Geschwister nachziehen** (Phase D, jetzt konkret): offene
`forge/*`-PRs, deren Basis sich bewegt hat, bekommen ein Merge des
Basis-Branches (nie Rebase/Force). Konflikt → Rework-Run (L1-Primitiv) mit
den Konfliktdateien als Kontext; nach `MAX_REWORK_ROUNDS` → `blocked`.

**Ressourcen pro Worktree:** eigene venv gibt es schon (`_venv.py`). Neu:
`gc_stale`/Disk-Limit vor jedem parallelen Dispatch prüfen; Tests, die feste
Ports öffnen, sind ein bekanntes Parallel-Risiko → `forge doctor` warnt, wenn
`max_parallel > 1` und die Eval-Suite keinen Port-/Tmp-Isolationshinweis hat.

**Events:** keine neuen Kinds. `WorkItemCreated` (A4) trägt Kanten und Dateien,
`WorkItemBlocked` nutzt den Grund `file_conflict` (`reason` ist schon
freier Text, kein Bump).
`ConductorTickCompleted` additiv `parallel_running: int` für den
Auslastungs-View.

**Bewusst nicht:** ein Runner, der selbst Worktrees aufspaltet und Subtasks
in mehreren Worktrees zusammenführt. Das würde den Runner zur
Orchestrierung machen (Mantra 3) und die Messbarkeit pro Knoten verlieren.
Wer feinere Parallelität will, zerlegt feiner (mehr Knoten), nicht tiefer.

---

## 5. Bewusst später

| Thema | Warum später |
|---|---|
| **Deploy / Post-Release-Verifikation** | Braucht ein Ziel-Umgebungsmodell (`deploy:`-Command in der Spec, trusted, + Smoke-Eval). Die Quelle „Post-Release" in A2 hängt daran. Erst sinnvoll, wenn L3 echte Versionen liefert. |
| **Weitere Anbieter** (GitLab, Gitea, Jira) | Nach P1: jeder ist ein Adapter, der die Vertrags-Suite besteht. |
| **Per-Rolle-Telemetrie** (`cost_per_role`) | Rohdaten liegen in `.forge/logs/*.jsonl`; Aggregation ist eigene Event-Logik. |
| **PBS / Bandit** (Spec v2/v3) | gegated auf ≥100 bzw. ≥300 Runs. |

---

## 6. Empfohlene Reihenfolge

| # | Schritt | Warum an dieser Stelle |
|---|---|---|
| 1 | **L0** Live-Verifikation | Alles baut auf den Tracker-/Host-Kanten auf. |
| 2 | **P0** Provider-Schnittstelle (Refactoring) | Jeder spätere Schritt fügt Tracker-/Host-Aufrufe hinzu. Vorher abstrahiert = nichts doppelt bauen. Behebt den `triage`-Grenzverstoß. |
| 3 | **L1** Nacharbeit + Branch-Primitiv | Häufigster Stillstand; Primitiv ist Basis für L2 und G. |
| 4 | **L2** CI-Autofix | Billig auf L1. |
| 5 | **P1** Azure DevOps | Jetzt deckt die Schnittstelle schon Review, CI-Status und Push auf Branch ab. Kann vorgezogen werden, wenn Azure dringender ist (E11). |
| 6 | **A1** Specs als Artefakte | Bessere Eingabe für alle späteren Stages, wenig Risiko (erzeugt keine Items). |
| 7 | **A2–A4** Work-Items erzeugen | Größte neue Angriffsfläche, deshalb mit Gate `forge:proposed` und opt-in. |
| 8 | **G** Arbeitsgraph + Konflikt-Scheduling | Braucht die Kanten und `Touches:` aus A. |
| 9 | **L3** Release-Train | Lohnt sich, sobald mehrere Items pro Woche durchlaufen. |

**Schema-Bilanz:** **+1 EventKind** (`WorkItemCreated`, 27). Additive Bumps:
`RunStarted` 1.1 (`trigger="rework"`, `provider`), `PRReviewed` 1.1
(`reasoning_blob`), `RequirementsRefined` 1.1 (`spec_path`, `spec_blob`),
`ReleaseTagged` 1.1 (`version`, `issue_numbers`), `ConductorTickCompleted` 1.2 (`parallel_running`). Keine
Breaking Changes. `scoring.py`/`gates.py`/Runner-Decide bleiben unangetastet
(Mantra 3). Alles Neue lebt in Loop 2 (`forge-cli`), in den Adaptern und in
additiven Spec-Feldern.

---

## 7. Entscheidungen für den Operator

| # | Frage | Vorschlag |
|---|---|---|
| E1 | L1: Soll das Item bei Nacharbeit sichtbar auf `forge:in-dev` zurückspringen oder in `qa` bleiben und dort in-place nachgearbeitet werden? | **Zurück nach `in-dev`** — der Board-Zustand zeigt, wer gerade arbeitet; der Übergang ist schon erlaubt. |
| E2 | L1/L2/G: Darf forge auf einen bestehenden PR-Branch pushen (nur `forge/*`, nur fast-forward bzw. Merge-Commit)? | **Ja**, mit Präfix-Guard; menschliche Branches nie. |
| E3 | L1/L2: Rundenlimits `MAX_REWORK_ROUNDS` / `MAX_CI_FIX_ATTEMPTS` als Konstante oder Spec-Feld? | Erst **Konstante = 2** (wie `MAX_DEV_RETRIES`), Spec-Feld, wenn Daten es rechtfertigen. |
| E4 | L3: Release-PR-Muster (Changelog im Repo, via PR) oder nur Tag + Release-Notes ohne Repo-Änderung? | **Release-PR** — einzig mit `push_to_main=false` verträglicher Weg zu einem versionierten Changelog im Repo. |
| E5 | L3: Tag-Schema `vX.Y.Z` und der heutige `forge-issue-<N>` daneben weiter? | `mode: train` → nur `vX.Y.Z`; `per_issue` bleibt Default für bestehende Setups. |
| E6 | A: Capability `create_work_items` — einverstanden, dass forge selbst Issues/Work Items anlegt? | **Ja, opt-in + Tages- und Epic-Obergrenze.** |
| E7 | A: Starten erzeugte Items in `forge:proposed` (Mensch gibt frei) oder direkt in `requirements`? | **`forge:proposed` per Default**, `auto_accept` pro Typ abschaltbar. |
| E8 | Deploy (§5) überhaupt im forge-Scope, oder endet die Fabrik bewusst beim Release? | Endet beim Release, bis L3 läuft; danach neu bewerten. |
| E9 | P: Stage in Azure DevOps als **Tag** `forge:<stage>` oder über **State**/Board-Spalte? | **Tag** — gleiche Semantik wie GitHub-Labels; States sind je Prozess-Template verschieden und gehören dem Team. |
| E10 | P: Azure-Zugriff über `az`-CLI (wie gh) oder direkt REST? | **`az`-CLI**, REST nur für Lücken — gleiches Test-Muster wie gh. |
| E11 | P: Wie dringend ist Azure DevOps — vor oder nach L1/L2? | Nach L1/L2 (Schritt 5). Wenn ein Azure-Projekt wartet: direkt nach P0. |
| E12 | A1: Feature-Specs als Datei im Repo (`docs/specs/`, via PR) oder nur als Kommentar am Work-Item? | **Datei via PR** für Features/Epics, Kommentar für Bugs/Tasks. |
| E13 | G: Integrations-Branch pro Epic als Option anbieten oder immer direkt gegen `main`? | **Option, Default `direct`.** Integrations-Branch nur für Epics, die nur als Ganzes ausgeliefert werden dürfen. |
| E14 | G: `Touches:` (erwartete Dateien) Pflicht für Parallelität, oder darf forge ohne Angabe parallelisieren? | **Pflicht** — ohne `Touches:` läuft ein Knoten allein. Sicher vor schnell. |


---

## 8. Umsetzungsstand

| # | Abschnitt | Stand | Wo |
|---|---|---|---|
| L0 | Live-Verifikation | ✅ Werkzeug da: `forge doctor --board [--fix]`, `board-loop --max-ticks`, `scripts/conductor-smoke.sh`. Der Lauf gegen ein echtes Sandbox-Board steht noch aus. | `doctor.py` |
| P0 | Provider-Schnittstelle | ✅ `WorkTracker`/`CodeHost`, Registry, Fakes, Vertrags-Suite, Grenzverstoß in `triage/` behoben (AST-Test) | `forge_adapters/base.py`, `registry.py`, `fake.py` |
| L1 | Nacharbeit | ✅ | `conductor.py`, `stages.py`, `board_loop._dispatch_branch_run` |
| L2 | CI-Autofix | ✅ | dito |
| P1 | Azure DevOps | ✅ Boards/Repos/Pipelines über `az`, Pipelines-Templates, Beispiel-Spec | `forge_adapters/azure/` |
| A | Arbeit erzeugen | ✅ Specs, Epics, Funde, Review-Folgeaufgaben, CI rot auf main, Schedules | `intake.py`, `workgen.py` |
| G | Arbeitsgraph | ✅ Touches-Konflikte, Kapazität, Sync, Integrations-Branch | `conductor.py`, `workgraph.py` |
| L3 | Release-Train | ✅ | `release.py` |

**Bewusste Abweichungen vom Entwurf**

- `RequirementsRefined` blieb auf 1.0: die Spec liegt schon als
  `artifacts["spec"]` im Event, der Spec-PR ist ein normales `PRCreated` mit
  Label `forge:spec`. Ein zusätzliches `spec_path`-Feld hätte nichts gebracht.
- `EpicDecomposed` entfiel zugunsten des generischen `WorkItemCreated`
  (Kanten + Eltern daraus rekonstruierbar) — wie in A4 vorgesehen.
- E14 präzisiert: ein Code-Run **ohne** `Touches:` läuft nie gleichzeitig mit
  einem anderen Code-Run; gegenüber bereits offenen PRs ohne `Touches:` gibt es
  keine Kante (sonst wäre jede Fabrik ohne Touches-Angaben strikt seriell) —
  Konflikte mit offenen PRs fängt das Nachziehen (`sync`) ab.
- Azure DevOps kennt kein Release-Objekt → Release = annotierter Git-Tag.
  PR-Diff und Head-Commit-Zeit liest der Adapter über git (die `az`-CLI
  liefert beides nicht), Review-Kommentare über `az devops invoke`.
- Merge-Konflikte: statt eines LLM-Runs für jeden Konflikt erst ein
  deterministischer Merge der Basis; nur echte Konflikte gehen an einen Agenten,
  der von einem lokal committeten Konflikt-Stand startet (rot→grün-Pfad).
- Zusätzlich (nicht im Entwurf, aus der Azure-Arbeit gefolgt): der Conductor
  trägt beim Code-Host beobachtete Merges als `PRMerged(merger="external")`
  nach — ersetzt anbieter-neutral den Webhook.

**Offen**

- Der echte Live-Durchstich gegen ein GitHub-Board und ein Azure-Projekt
  (`scripts/conductor-smoke.sh`). Bis dahin sind alle Anbieter-Kanten nur gegen
  CLI-Simulatoren verifiziert.
- Release-Notes-Agent (optional, fail-open) — der Changelog ist rein
  deterministisch.
- Deploy/Post-Release (§5) unverändert offen (E8).
