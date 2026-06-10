# forge Conductor — Design (Loop 2: die Fabrik-Ebene)

> Status: **Entwurf** · 2026-06-04 · betrifft `forge-cli` (board-loop → conductor),
> `forge-core` (neue Event-Kinds), `forge-adapters` (Board-Transitions).
> Dieses Dokument ist Design, kein Vertrag — der Vertrag ist die Spec. Wenn ein
> Vorschlag hier einem der drei Mantras widerspricht, gewinnt das Mantra.

## 0. Worum es geht

forge v1 optimiert **innerhalb** eines Work-Items: ein Issue → ein Run → mehrere
Generationen → KEEP → PR. Die „Teams" (architect → developer → tester → reviewer)
sind Rollen *innerhalb eines* `propose()`-Aufrufs.

Das Ziel ist eine **Software-Factory**, in der mehrere Teams **koordiniert über
Zeit** an *einem Produkt* arbeiten — requirements, architecture, dev, test, qa,
devops. Das ist eine Ebene höher: nicht *intra-issue*, sondern *inter-issue*. Die
Einheit ist nicht mehr der Run, sondern der **Produkt-Backlog über die Zeit**.

Heute fehlt diese Ebene fast vollständig:

- `board-loop` ist ein **einmaliger, sequenzieller Batch-Durchlauf**
  (`board_loop.py:241`) — kein Dauerbetrieb, keine Abhängigkeiten, kein
  Stage-Übergang.
- `schedule`-Trigger, `on_ci_failure`, `ReleaseConfig`, der `operations`-Subagent:
  als Config **reserviert**, **kein Executor** dahinter.
- Issues sind **atomare, unabhängige Einheiten** — kein Graph, keine Reihenfolge.

Der **Conductor** schließt diese Lücke. Er ist „Loop 2": die Schicht, die Loop 1
(den Runner) orchestriert.

## 1. Position in der Architektur

```
                ┌─────────────────────────────────────────────┐
   Loop 2  ────►│  CONDUCTOR  (Heartbeat · State-Machine ·     │
  (Fabrik)      │  Dependency-Graph · Scheduler)              │
                └───────────────┬─────────────────────────────┘
                                │ dispatch (execute_run)
                                ▼
   Loop 1  ────►   SequentialRunner  (propose → mutate → eval → decide)
  (ein Run)                     │
                                ▼
                Master-claude  (architect → developer → tester → reviewer)
```

**Mantra 3 ist die tragende Bedingung.** „Loop berührt seine eigene Loop-Logik
nie." Der Conductor steht **über** der Loop, nie darin:

- Er liest den Event-Strom und den Board-Zustand und **dispatched** Runs. Er
  greift nie in den Runner, das Scoring oder die Gates ein.
- Er schedult **Arbeit** (Work-Items), nicht **Loop-Logik**. Er ändert forges
  Konfiguration NIE zur Laufzeit (Self-Improvement bleibt ausgeschlossen).
- Konkret: der Conductor ist **der Operator als Software** — exakt die Rolle, die
  heute der Mensch einnimmt, der `board-loop` aufruft, Labels setzt und Issues
  in Reihenfolge bringt.

Würde diese Logik in `forge-execute` wandern, müsste die Loop die Fabrik kennen →
Mantra-3-Bruch. Deshalb lebt der Conductor in der CLI-/Adapter-Schicht.

## 2. Kernbegriffe

| Begriff | Bedeutung |
|---|---|
| **Work-Item** | Eine Arbeitseinheit = ein GitHub-Issue. Hat eine **Stage** und 0..n **Dependencies**. |
| **Stage** | Die aktuelle Phase eines Work-Items im Fließband (requirements → … → done). Als Label kodiert. |
| **State-Machine** | Die erlaubten Stage-Übergänge + wer sie auslöst (Event-getrieben). |
| **Dependency-Graph** | Gerichtete „blockiert-durch"-Kanten zwischen Work-Items. Bestimmt die Ready-Reihenfolge. |
| **Tick** | Ein Conductor-Zyklus: Board lesen → Stages fortschreiben → Ready-Queue auflösen → dispatchen → schlafen. |
| **Heartbeat** | Die Endlosschleife aus Ticks — der „rund um die Uhr"-Betrieb. |
| **Roster/Team** | Die Subagent-Rollen, die eine Stage bearbeiten (bestehende `agents:[...]`-Config pro Label). |

## 3. Die Stage-State-Machine

Stages sind **Labels** auf dem Issue. Das ist bewusst: Labels sind menschlich
sichtbar, im GitHub-UI editierbar (Operator-Override jederzeit), und sie sind
**genau die Trigger-Keys**, die `on_issue_label` schon heute auf Rollen-Roster
mappt. Stage-Label und Trigger-Label fallen zusammen — kein zweiter Konfig-Ort.

```
forge:requirements ─► forge:design ─► forge:ready ─► forge:in-dev
                                                          │
                                                          ▼
   forge:done ◄─ forge:release ◄─ forge:qa ◄─────────────┘
```

| Stage | Team/Roster | Conductor schreibt fort, wenn … |
|---|---|---|
| `forge:requirements` | operations/requirements | … ein klar spezifiziertes, akzeptanz-fähiges Issue entstand → `design` |
| `forge:design` | architect (Produkt-Ebene) | … ein Plan/ADR existiert (PlanProposed, nicht „Insufficient context") → `ready` |
| `forge:ready` | — (Warteschlange) | … alle Dependencies `done` UND Kapazität frei → `in-dev` (dispatch) |
| `forge:in-dev` | architect/developer/tester/reviewer | … der Run einen PR öffnete → `qa`; bei `no_improvement`/Fehler → zurück oder eskalieren |
| `forge:qa` | reviewer + judge | … der PR gemerged wurde → `release` |
| `forge:release` | devops | … Tag/Changelog erzeugt (on_main_green) → `done` |
| `forge:done` | — | terminal |

**Wer löst Übergänge aus?** Ausschließlich der Conductor, und nur auf Basis von
**Events** (Mantra 2) bzw. beobachtetem GitHub-Zustand:

- `in-dev → qa`: es existiert ein `PRCreated`-Event für dieses Work-Item.
- `qa → release`: es existiert ein `PRMerged`-Event (vom Webhook-Adapter).
- `design → ready`: es existiert ein `PlanProposed` mit `insufficient_context=false`.

Der Conductor **errechnet** den Soll-Zustand aus den Events und **effektiert** die
Differenz über den Board-Adapter (Label setzen/entfernen). Das ist idempotent:
derselbe Event-Stand führt immer zur selben Stage. Damit ist die Fabrik
**replay-fähig** wie die Loop.

**Operator-Override gewinnt.** Setzt ein Mensch ein Label von Hand, respektiert der
Conductor das (er überschreibt nie einen manuellen Eingriff im selben Tick — er
liest den Ist-Zustand und rechnet vorwärts). Eskalations-Stages (`forge:blocked`,
`forge:needs-human`) nimmt der Conductor nie selbst zurück.

## 4. Dependency-Modell

Koordination ohne Reihenfolge ist nur Parallelität. Work-Items brauchen Kanten.

**v1-Quelle (deklarativ, geparst):** Eine Zeile im Issue-Body:

```
Depends-On: #12, #15
```

Plus — falls die GitHub-API es liefert — native **sub-issue/blocked-by**-Relationen.
Beides wird zum gerichteten Graph zusammengeführt. Bewusst **kein** LLM-Inferenz
von Abhängigkeiten in v1: der Graph muss deterministisch und replay-fähig sein.

**Scheduling-Regel:** Ein Work-Item wird nur dann `ready → in-dev` befördert, wenn
**alle** seine Dependencies in Stage `done` sind. Zyklen werden erkannt und als
`forge:blocked` + Event geflaggt (nie still verschluckt). Die Ready-Reihenfolge ist
ein topologisches Sort + Tie-Break nach (Priorität-Label, Issue-Alter).

## 5. Der Heartbeat (Tick-Anatomie)

Ein Tick ist deterministisch und kurz; die Arbeit passiert im dispatchten Run.

```
tick():
  1. PULL    Board-Zustand + relevante Events seit letztem Tick lesen.
  2. ADVANCE Stage-State-Machine: Soll-Stage je Work-Item aus Events ableiten,
             Differenz via Board-Adapter effektieren (Label-Transitions).
             → WorkItemStageChanged-Events emittieren.
  3. SCHEDULE Fällige schedule-Trigger (cron) auswerten → Work-Items enqueuen.
  4. RESOLVE  Ready-Queue bilden: Stage==ready ∧ alle Deps done ∧ Kapazität frei.
             Topologisch + Priorität sortieren.
  5. DISPATCH Bis Kapazität erschöpft: execute_run() je Work-Item (wie board-loop
             heute). Roster via _roster_for_issue. Bei cost_cap/guardrail/error:
             Work-Item flaggen, nicht den ganzen Heartbeat killen.
  6. EMIT     ConductorTickCompleted (queued, dispatched, blocked, skipped).
  7. SLEEP    bis zum nächsten Intervall ODER bis ein Webhook den Tick weckt.
```

**Robustheit (24/7):**

- **Graceful shutdown** auf SIGINT/SIGTERM — laufenden Run zu Ende bringen, dann
  stoppen. Der Heartbeat ist resumierbar: nach Neustart rekonstruiert Schritt 2
  alle Stages aus Events (kein verlorener Zustand).
- **Backpressure:** leere Ready-Queue → nur schlafen, kein Leerlauf-Spend.
- **Globale Caps:** die bestehenden `cost_caps.per_project_per_day/month` sind die
  harte Ressourcen-Grenze über den Dauerbetrieb (kein neuer Mechanismus nötig).
- **Webhook-Wakeup (optional):** der `forge-adapters`-Webhook kann den Heartbeat
  früher ticken lassen (PR merged → sofort `qa → release`), statt aufs Intervall
  zu warten. Fällt der Webhook aus, holt der nächste Zeit-Tick alles nach — der
  Tick ist die Wahrheit, der Webhook nur Beschleuniger.

## 6. Schedule-Trigger

`ScheduleTriggerConfig` (cron + focus) ist heute totes Config. Der Conductor macht
es lebendig: in Schritt 3 jedes Ticks werden die Cron-Ausdrücke gegen „jetzt"
ausgewertet; ist einer seit dem letzten Lauf fällig, wird ein Work-Item mit dem
`focus` enqueued. Der letzte Lauf-Zeitpunkt wird aus dem Event-Strom rekonstruiert
(idempotent über Neustarts), nicht in einer Seitendatei gehalten.

## 7. Nebenläufigkeit — bewusst gegated

Die Vision will mehrere Teams *gleichzeitig*. Die Spec schließt Parallelität in v1
**kategorisch** aus (Population-Based-Search erst ab ≥100 Runs + dokumentiertem
Plateau). Auflösung der Spannung durch eine saubere Unterscheidung:

- **Durchsatz-Parallelität** (verschiedene Work-Items in isolierten Worktrees
  parallel) ist eine Fabrik-Sache. Die Worktree-Isolation existiert bereits.
- **Such-Parallelität** (mehrere Strategien für *dasselbe* Item — PBS) ist die
  gegatete Optimierung.

**Conductor v1 fährt mit `max_concurrency=1`** (sequenziell, wie board-loop heute) —
aber die Architektur ist nebenläufigkeitsbereit: die Ready-Queue + ein
Kapazitäts-Semaphor. Sobald die Factory-KPIs (siehe `factory_kpis`-View) einen
stabilen sequenziellen Durchsatz zeigen, wird `max_concurrency` angehoben. Das ist
„erst messen, dann skalieren" in Reinform — die Daten dafür liefert Phase A
(bereits gebaut).

## 8. Events (Mantra 2)

Jeder Conductor-Schritt ist ein Event. Neue Kinds (jeweils `register_payload`,
eigene Datei in `events/kinds/`, Schema-Versionsdisziplin aus CLAUDE.md beachten —
`len(EventKind)`-Test mitziehen):

| EventKind | Payload (Kern) | Wann |
|---|---|---|
| `WorkItemStageChanged` | `issue_number`, `from_stage`, `to_stage`, `reason` | Stage-Übergang in Schritt 2 |
| `WorkItemBlocked` | `issue_number`, `blocked_by: list[int]`, `kind` (`deps`/`cycle`/`error`) | Item kann nicht voran |
| `ConductorTickCompleted` | `queued`, `dispatched`, `blocked`, `skipped`, `duration_ms` | Ende jedes Ticks |

**Offene Designfrage — Event-Envelope:** Das heutige Event-Modell ist run-zentrisch
(`run_id` pflicht). Conductor-Events stehen *über* Runs. Vorschlag (minimal-invasiv,
**keine** Envelope-Chirurgie): der Heartbeat-Prozess bekommt eine
**Conductor-Session-ULID**, die als `run_id` dieser Events dient; das Work-Item
referenziert die `issue_number` im Payload. So bleibt `forge replay`/der Store
unverändert. Alternative (Envelope um optionales `work_item`-Feld erweitern) wäre
sauberer, aber teurer — als Folge-Entscheidung markiert, nicht in v1.

## 9. Wo der Code lebt

`board-loop` liegt heute in `forge-cli` und nutzt `execute_run` (forge-cli) +
`forge-adapters` (Board) + `forge-core` (Store). Der Conductor ist seine Evolution.

**v1-Empfehlung: in `forge-cli`** als neues Modul `conductor.py` neben
`board_loop.py`, mit klarer interner Naht (Tick-Funktion, State-Machine, Scheduler
je eigene, testbare Einheit). Begründung: kein verfrühter Package-Split, maximale
Wiederverwendung des bestehenden `execute_run`-Pfads, gleiche Boundary wie
board-loop (die Mantra-3-Trennung ist konzeptionell, nicht Package-gebunden — die
CLI-Schicht darf orchestrieren, `forge-execute` nicht).

**Graduationspfad:** Wächst der Conductor über die CLI hinaus (eigener Daemon,
mehrere Repos), wandert die Tick-/State-Machine-Logik in ein eigenes Package
`forge-conduct` (hängt an `forge-core` + `forge-adapters`; der Run-Dispatch
`execute_run` müsste dafür von `forge-cli` nach `forge-execute` runterwandern). In
v1 nicht nötig.

## 10. Was der Conductor NICHT tut

Kategorisch ausgeschlossen, gleiche Linie wie die Spec-v1-Ausschlüsse:

- **Kein Auto-Merge durch forge selbst.** `qa → release` setzt voraus, dass der PR
  *bereits* gemerged ist (Mensch oder GitHub-Auto-Merge-Feature). Der Conductor
  merged nie.
- **Keine Self-Improvement.** Er ändert nie forges Config, Roster-Definitionen oder
  seine eigene Scheduling-Logik zur Laufzeit.
- **Keine LLM-Dependency-Inferenz** (v1: deklarativ/geparst).
- **Keine Such-Parallelität / PBS** (gegated auf Daten).
- **Keine Merge-Queue-/Konflikt-Auflösung über mehrere PRs** (Phase D).
- **Kein Zurücknehmen menschlicher Eskalations-Labels.**

## 11. Phasenplan

| Phase | Inhalt | Liefert |
|---|---|---|
| **A — Sehen** ✅ | `factory_kpis` + `factory_throughput` Views, `forge analyze` | Durchsatz/Merge-/Keep-Rate/Lead-Time messbar |
| **B — Heartbeat** | `board-loop --watch --interval N` (Dauerbetrieb) + `schedule`-Trigger verdrahten. **Noch keine** State-Machine/Deps. | „Rund um die Uhr" wird real |
| **C — Conductor** | Stage-State-Machine + Dependency-Graph + Conductor-Events, auf dem Heartbeat aufbauend | Koordiniertes Fließband |
| **D — Skalieren** | `max_concurrency>1` + Merge-Queue + Integrations-Run. Gegated durch Phase-A-KPIs. | Mehrere Teams gleichzeitig |
| **E — Optimieren** | Population-Based-Search / Bandit (v2/v3 der Spec) | Strategie-Auswahl aus Daten |

**Diese Session implementiert B, dann C.** D/E bleiben datengetrieben offen.

### Phase B — konkrete Schritte (Heartbeat) — **umgesetzt**

1. ✅ `forge board-loop --watch --interval <s>`: umschließt den (extrahierten)
   Board-Pass mit einer Endlosschleife + Sleep + Graceful-Shutdown (SIGINT/
   SIGTERM → Stop-Flag, zwischen Ticks geprüft, laufender Run wird zu Ende
   gebracht). Engine: `heartbeat.py::run_heartbeat`, mit injizierten Deps
   (sleep/should_stop/emit) voll testbar.
2. ✅ `ConductorTickCompleted`-Event pro Tick (Session-ULID als `run_id`).
3. ✅ Cron-Matcher `schedule.py` (dependency-frei, 5-Feld, Vixie-dom/dow-ODER),
   voll unit-getestet — die **Maschinerie** für `schedule`-Trigger.
4. ✅ Tests: Heartbeat (max_ticks / Stop-Signal / stop_on_bail / emit),
   Cron (parse/match/due-Fenster), Watch-Glue (Ticks + Event-Persistenz +
   Board-Fehler-Robustheit).

5. ✅ Schedule-Dispatch (nachgereicht, Production-Hardening): die fehlende
   Prompt-Quelle ist entschieden — `ScheduleTriggerConfig.prompt_file`
   (repo-relative, operator-geschriebene Datei = trusted Auftragstext).
   `_dispatch_due_schedules` feuert fällige Trigger in beiden Watch-Modi vor
   dem Board-Pass; `last`-Anker = jüngstes `RunStarted(trigger=schedule,
   focus=…)` aus dem Event-Strom (`EventStore.last_run_started`), Fallback
   Session-Start (kein Massen-Feuern beim Heartbeat-Start). Ohne
   `prompt_file`: Skip + Spec-Warnung + Doctor-Check — kein leerer LLM-Call.

### Phase C — konkrete Schritte (Conductor)

**Kern (umgesetzt, voll getestet):**

1. ✅ Event-Kinds `WorkItemStageChanged` + `WorkItemBlocked` (19→21) + Schema-Test.
2. ✅ `stages.py`: `Stage`-Enum + `ALLOWED_TRANSITIONS` + `stage_of(labels)` +
   `advance(stage, signals)` (rein: event-getriebene Auto-Übergänge).
3. ✅ `dependencies.py`: `parse_depends_on(body)` + `find_cycle` +
   `unmet_dependencies` (rein, replay-fähig — keine LLM-Inferenz).
4. ✅ `conductor.py`: `plan_tick(items, capacity)` = ADVANCE → CYCLES → RESOLVE →
   DISPATCH (genau ein Übergang pro Item pro Tick; at-most-once Dispatch).
   `derive_signals(events, issue)` leitet Plan/PR/Merge rein aus dem Event-Strom
   ab (Korrelation über `RunStarted.issue_number`). `run_conductor_tick` effektiert
   den Plan über **injizierte** Callables (set_stage/dispatch/on_blocked) — erst
   Labels, dann Dispatch.
5. ✅ Tests: State-Machine, Dependency-Scheduling + Zyklen, Kapazitäts-Limit,
   Signal-Ableitung, Effekt-Reihenfolge (Label vor Dispatch).

**Integration (umgesetzt, gh-CLI gegen Stubs getestet):**

6. ✅ Board-Adapter: `list_stage_items(state="all")` (Issues über ALLE
   `forge:`-Stages, inkl. `done`/closed für Dependency-Auflösung) +
   `set_issue_stage_label(add, remove)` (idempotent) — gh-CLI wie `board.py`,
   mit gestubbtem Subprocess getestet.
7. ✅ CLI: `board-loop --watch --conductor` — baut pro Tick die `WorkItem`-Liste
   (Stage aus Labels, Deps aus Body, Signale aus Events) und ruft
   `run_conductor_tick`; `set_stage` effektiert das Label via gh + emittiert
   `WorkItemStageChanged`, `on_blocked` emittiert `WorkItemBlocked`, `dispatch`
   nutzt den bestehenden `_dispatch_issues`/`execute_run`-Pfad. Die gemeinsame
   Heartbeat-Mechanik (Session-ULID, Signal-Shutdown, Tick-Event) teilen sich
   board-watch und conductor-watch in `_heartbeat_session`.

Der Kapazitäts-Semaphor steht in v1 auf 1 (sequenziell, Daten-Gate). Die
gh-Kommandos sind gegen Stubs verifiziert (gleicher Standard wie der bestehende
Board-Adapter) — die **Live-Verifikation gegen ein echtes Board mit
`forge:`-Labels** steht noch aus und ist der nächste Schritt vor Produktiv-Nutzung.

**Stage-spezifischer Dispatch (umgesetzt — der „verschiedene-Teams"-Kern):**

Bis hierher dispatchte der Conductor *eine* Sorte Arbeit: `ready→in-dev` (den
Dev-Loop → PR). Damit lief nur ein Team. Jetzt entscheidet die **Stage** über
das **Team** und die **Run-Art**:

- `stages.IN_PLACE_WORK_STAGES` (= `{design}`) markiert Stages, in denen ein
  Team *in-place* arbeitet und seinen Advance-Auslöser produziert, ohne dass das
  Item die Stage wechselt. `plan_tick` dispatcht so ein Item (dependency-gegated,
  gemeinsame Kapazität mit `ready`), `advance` schreibt es nächsten Tick fort.
- `TickPlan.dispatch` ist jetzt `list[DispatchOrder]` (`number` + `stage`) statt
  `list[int]` — der Dispatch trägt sein Team mit. Die Wiring-Schicht
  (`_run_conductor_watch`) verzweigt: `design` → `_dispatch_design_run`
  (architect-Roster, `create_pr=False`, Output = `PlanProposed` → `has_plan` →
  `design→ready`), `in-dev` → der bestehende Dev-Loop (`_dispatch_issues`,
  `create_pr=True`, Output = PR).
- Roster pro Stage: `triggers.on_issue_label["forge:<stage>"].agents`
  (Stage-Label = Trigger-Key, ein Konfig-Ort); Default für `design` ist
  `["architect"]`.

Damit läuft erstmals ein **Zwei-Team-Fließband** (Design-Team → Dev-Team) rein
event-getrieben. `requirements` und `release` bleiben offen: sie bräuchten je ein
neues Advance-**Signal** (`StageSignals` + `advance`) und einen Dispatch-Zweig —
`requirements` ein „Spec fertig"-Signal, `release` ein „Tag/Changelog erzeugt"-
Signal. Beide kommen mit Inkrement 2 (eigenes Design der Output-Verträge).

**Eskalation (nachgereicht, Production-Hardening):** ein `in-dev`-Item, dessen
jüngster Run ohne `PRCreated` endete (bzw. ein `design`-Item ohne verwertbaren
Plan), eskaliert via `advance` nach `forge:blocked` —
`StageSignals.last_run_finished_without_pr/_plan`, abgeleitet aus
`RunStarted`+`RunFinished` des jüngsten Runs. Event-Spur: `WorkItemStageChanged`
+ `WorkItemBlocked(kind="stalled")`. Re-Dispatch bleibt Operator-Entscheidung
(Label zurück auf `ready`) — kein stiller Endlos-Retry, aber auch kein
lautloses Liegenbleiben mehr.

**Operator-Setup (für die Live-Erprobung):** Issues mit `forge:`-Stage-Labels
versehen (`forge:design`/`forge:ready`/…), Dependencies via `Depends-On: #N` im
Body, dann `forge board-loop --watch --conductor --interval <s>`. Stage-Labels
in GitHub vorab anlegen (gh erstellt sie sonst nicht automatisch).

## 12. Zu bestätigende Entscheidungen

1. **Stage-Label-Namespace:** `forge:<stage>` vs. ein konfigurierbares Präfix?
   (Vorschlag: `forge:`-Präfix, in der Spec überschreibbar.)
2. **Event-Envelope:** Conductor-Session-ULID als `run_id` (minimal) vs.
   Envelope-Erweiterung (sauberer)? (Vorschlag: Session-ULID in v1.)
3. **Dependency-Quelle:** nur `Depends-On:`-Body-Zeile, oder auch native
   GitHub-sub-issues, falls die API im Scope-Repo verfügbar ist?
4. **Code-Ort:** `forge-cli/conductor.py` (v1) bestätigt, oder direkt `forge-conduct`?
```
