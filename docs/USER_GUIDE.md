# forge — User Guide

Diese Anleitung richtet sich an **Anwender**, die forge in einem eigenen Repo
einsetzen wollen. Für die Weiterentwicklung von forge selbst siehe
[`CONTRIBUTING.md`](../CONTRIBUTING.md) und [`CLAUDE.md`](../CLAUDE.md); der
verbindliche Vertrag ist [`docs/forge-spec-v0.6.md`](forge-spec-v0.6.md).

---

## 1. Mentales Modell in 60 Sekunden

forge ist eine **Auto-PR-Maschine**: du gibst ein Ziel vor (ein Issue, einen
Prompt, ein rotes Test-Gate), forge lässt einen Claude-Agenten (oder ein
orchestriertes Team) in einem **isolierten Git-Worktree** daran arbeiten,
**misst** das Ergebnis gegen deine Gates, **behält** nur, was die Messung
verbessert, und öffnet bei Erfolg einen **Pull Request**. Du reviewst und
merged. Jeder Schritt wird als Event protokolliert.

Zwei Ebenen:

- **Loop 1 — ein Run** (`forge run`): genau eine Aufgabe von Anfang bis PR.
- **Loop 2 — die Fabrik** (`forge board-loop`): taktet viele Issues durch eine
  Stage-Pipeline und dispatcht pro Stage einen Run.

Drei Sicherheits-Grundsätze, die du als Anwender kennen musst:

1. forge editiert **nur** Pfade, die du in `surfaces` freigibst — und **nie**
   Pfade in `forbidden`.
2. forge **merged nie selbst** (`merge_pr`/`push_to_main` sind hartkodiert aus).
3. Jeder Run hat **Cost-Caps**. Ohne sie läuft nichts.

---

## 2. Voraussetzungen

- Python ≥ 3.12, [`uv`](https://docs.astral.sh/uv/), Git
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) — für echte
  Runs. Auth über `claude /login` (Subscription) **oder** `ANTHROPIC_API_KEY`.
- [`gh`](https://cli.github.com/) — für PR-Erzeugung und das Project-Board.

`forge doctor` prüft all das (siehe unten).

---

## 3. Erstes Setup in einem Repo

```bash
cd /pfad/zu/deinem/repo

# 1. Rudimentäre Config erzeugen (idempotent — überschreibt nichts)
forge init

# 2. Config an dein Projekt anpassen (siehe Abschnitt 4)
$EDITOR .forge/project.yaml

# 3. Validieren
forge doctor --spec .forge/project.yaml
```

`forge init` legt ein **rudimentäres** `.forge/project.yaml` mit sicheren
Defaults an (`merge_pr: false`, `push_to_main: false`, ein `cost_caps`-Block).
Es ist bewusst minimal — du musst Surfaces, Gates und Eval-Suiten selbst
ergänzen, damit `forge doctor` grün wird und Runs sinnvoll messen können. Die
Reference-Spec [`examples/pinta/.forge/project.yaml`](../examples/pinta/.forge/project.yaml)
ist die beste Vorlage.

---

## 4. Die `project.yaml` schreiben

Die Spec ist der **Vertrag**. Die wichtigsten Blöcke:

### surfaces — was forge ändern darf

Bewusst **eng** schneiden. Lieber eine neue Surface als eine bestehende
ausweiten.

```yaml
surfaces:
  backend_services:
    paths: ["backend/src/services/"]
    type: code
    guardrails:                       # schnelle Preflight-Checks (Budget <30s)
      - "black --check backend/src/services/"
```

### forbidden — was forge NIE anfasst

Feiner als Surfaces; **gewinnt** bei Overlap. Hier gehören Auth, Payments,
DB-Migrationen, Secrets, CI und forge-eigene Dateien hin.

```yaml
forbidden:
  - "backend/src/routes/auth.py"
  - "backend/alembic/**"
  - ".forge/**"
  - ".github/workflows/**"
```

> Glob-Overlap mit Surfaces ist erlaubt: `surfaces: ["src/**"]` +
> `forbidden: ["src/secrets.py"]` ist OK. Exakte String-Gleichheit ist verboten.
> Ein Trick: lege einen Akzeptanztest in `forbidden` — das Eval-Gate führt ihn
> aus, der Agent darf ihn aber nicht abschwächen.

### capabilities — erlaubte Aktionen

```yaml
capabilities:
  read: ["**/*"]
  edit: ["{surfaces}"]                # Platzhalter: alle surface-Pfade
  run: ["pytest *", "black *", "uv run pytest*"]
  commit: true
  open_pr: true
  merge_pr: false                     # hardcoded — Änderung wird abgelehnt
  push_to_main: false                 # hardcoded
  network_egress: ["api.anthropic.com", "api.github.com"]
```

### eval_suites + gates — wie gemessen wird

Eval-Suiten führen Kommandos aus und parsen das Ergebnis; Gates sind harte
Pass/Fail-Schwellen, die eine Generation blocken.

```yaml
eval_suites:
  quick:
    cmd: "pytest tests/ -q --tb=no"
    budget_s: 180
    parses: pytest_json               # parst die pytest-Summary-Zeile

gates:
  - {kind: pytest_pass_rate, threshold: 1.0, source: quick}
```

### cost_caps — Pflicht ab Tag eins

```yaml
cost_caps:
  per_generation_usd: 0.50
  per_run_usd: 1.50                   # konservativ starten, später lockern
  per_project_per_day_usd: 10.00
  per_project_per_month_usd: 100.00
```

### triggers — Modell & Roster pro Issue-Label

```yaml
triggers:
  on_issue_label:
    auto-fix:
      strategy: sequential
      model: sonnet
      max_iterations: 5
    auto-feature:
      strategy: sequential
      model: opus
      max_iterations: 10
      agents: [architect, developer, tester]
```

Optionale Blöcke: `scores` (kontinuierliche Metriken für Composite-Optimierung),
`diagnostics` (nur geloggt), `judge` (opt-in LLM-Verifikation, siehe Abschnitt 7),
`triage` (opt-in LLM-Vorfilter für Issues), `board` (für die Fabrik, Abschnitt 8),
`release` (Tagging-Verhalten).

---

## 5. Einen einzelnen Run starten (`forge run`)

```bash
# Issue-getrieben (Acceptance = Issue-Body)
forge run --trigger issue_label --issue 42 --create-pr

# Ad-hoc mit Prompt
forge run --prompt "Implementiere Volltext-Suche im QuoteService" \
  --multi-agent --model sonnet --max-iterations 5 --create-pr
```

Wichtige Flags:

| Flag | Wirkung |
|---|---|
| `--multi-agent` | Default-Team architect→developer→tester (statt Single-Agent) |
| `--agents a,b,c` | Explizites Roster. Bekannt: `architect, developer, tester, simplify, reviewer`. `--agents developer` = klassischer Single-Agent-Run |
| `--dry-run` | Mock-Agent statt Claude — validiert die Pipeline für $0 |
| `--create-pr` | Bei Erfolg `gh pr create` (kein Auto-Merge) |
| `--max-iterations N` | Anzahl Generations (Default 3) |
| `--max-turns N` | Tool-Turns pro Generation (Default 8; für Multi-Agent 30+) |
| `--agent-timeout S` | Wallclock-Limit pro Propose (Default 3600 Multi, 600 Single) |
| `--resume <run_id>` | Einen vom Session-Limit unterbrochenen Run fortsetzen |

Beginne **immer** mit `--dry-run`, um Spec + Worktree + Eval ohne Claude-Kosten
zu prüfen.

---

## 6. Features bauen — die rot→grün-Regel

**Das ist die wichtigste Eigenheit von forge.** Die Decide-Phase behält eine
Generation nur, wenn

1. ein Gate von **rot auf grün** springt (`gate_revival`), **oder**
2. ein **Composite-Score** sich messbar verbessert.

**Konsequenz:** Bei leeren `scores` und bereits grüner Baseline wird eine neue,
ebenfalls grüne Generation als `no_significant_change` **verworfen** — selbst
wenn der Code korrekt ist. Ein Bug-Fix für einen *vorher roten* Test ist
unproblematisch (rot→grün). Ein **neues Feature** auf grüner Baseline braucht
einen der folgenden Wege:

- **TDD-Anker (empfohlen):** Schreibe einen fehlschlagenden Akzeptanztest
  *vorher* und committe ihn. Baseline ist damit rot; der Agent macht ihn grün →
  KEEP. Lege den Test in `forbidden`, damit der Agent ihn nicht abschwächt — das
  Eval-Gate führt ihn trotzdem aus.
- **LLM-Judge:** Aktiviere die opt-in Judge-Phase (Abschnitt 7). Der Judge
  erzeugt den Messwert `llm_judge_score`; ohne ihn ist das zugehörige Gate rot,
  nach `pass` grün → derselbe rot→grün-Pfad.
- **Score hinzufügen:** Definiere einen `scores`-Eintrag (z.B. Coverage), den
  das Feature verbessert.

Wenn ein korrektes Feature „spurlos verworfen" wird, ist fast immer diese Regel
der Grund.

---

## 7. LLM-Judge (opt-in Verifikation gegen Akzeptanzkriterien)

Für Feature-Issues ohne natürliche Metrik. Der Judge bewertet den Diff
(read-only) gegen die Akzeptanzkriterien und liefert `llm_judge_score ∈ [0,1]`.
**Fail-closed**: scheitert er, bleibt das Gate rot (nichts Unverifiziertes wird
behalten).

```yaml
judge:
  enabled: true
  model: claude-haiku-4-5      # klein reicht — Bewertung, kein Code
gates:
  - {kind: llm_judge_score, threshold: 0.8}
```

Kriterien übergeben: `forge run --acceptance-file kriterien.md` (oder im
board-loop der Issue-Text). Siehe [`docs/forge-spec-v0.5.md`](forge-spec-v0.5.md).

---

## 8. Die Fabrik betreiben (`forge board-loop` + Conductor)

Für viele Issues. Voraussetzung: ein GitHub Project Board und ein `board:`-Block
in der Spec.

```yaml
board:
  provider: github
  owner: dein-user
  project_number: 4            # aus `gh project list --owner dein-user`
  filter_status: "Todo"
  filter_labels: ["bug"]
```

```bash
# Einmal-Pass: ready-Items ziehen und dispatchen
forge board-loop

# Kontinuierlich takten
forge board-loop --watch --interval 60 --max-parallel 2

# Mit Conductor-Stage-Pipeline (requirements → … → done)
forge board-loop --watch --conductor
```

Der **Conductor** bewegt Issues über Stage-Labels (`forge:requirements`,
`forge:design`, `forge:ready`, `forge:in-dev`, `forge:qa`, `forge:release`,
`forge:done`, `forge:blocked`) und dispatcht pro Stage das passende Team. Er
entscheidet rein aus dem Event-Strom (Mantra 3). Details:
[`docs/conductor-design.md`](conductor-design.md).

`--auto-merge` aktiviert GitHubs serverseitiges Auto-Merge (`gh pr merge
--auto`) — forge selbst merged weiterhin nicht; GitHub merged, sobald alle
required Checks grün sind. Bewusst opt-in pro Aufruf.

---

## 9. Beobachten & auswerten

```bash
forge watch [RUN_ID]     # Live-Panel: Tool-Calls, Subagent-Aktivität, Cost/Turns
forge analyze            # KPIs über alle Runs + Lessons Learned
forge replay <run_id>    # Run als lesbare Markdown-Timeline
```

`forge analyze` aggregiert read-only über alle Runs: Durchsatz, Merge-Rate,
Keep-Rate, Cost pro gemergtem PR, Lead-Time und destillierte „Lessons Learned".

Artefakte eines Runs liegen unter `.forge/`:

| Artefakt | Ort |
|---|---|
| Event-Store (DuckDB) | `.forge/events.duckdb` |
| CAS-Blobs (Prompts, Diffs) | `.forge/blobs/` |
| Live-Stream-Logs | `.forge/logs/<run_id>/*.jsonl` |
| Git-Worktree pro Run | `.forge/worktrees/<run_id>/` |

---

## 10. Einen unterbrochenen Run fortsetzen

Läuft ein Run in ein Claude-Usage-/Session-Limit, ist das **kein Fehlschlag**:
forge sichert die Partial-Arbeit als `forge: WIP`-Commit und legt einen
Resume-Anker (`RunResumeScheduled`). Fortsetzen:

```bash
forge run --resume <run_id>
```

Der Worktree bleibt eingefroren erhalten; `claude --resume` macht mit vollem
Kontext weiter. Im `board-loop --watch --conductor` werden fällige Resumes
automatisch dispatcht, sobald die Reset-Zeit erreicht ist.

---

## 11. Troubleshooting

| Symptom | Ursache / Lösung |
|---|---|
| Korrektes Feature wird verworfen (`no_improvement`) | Die rot→grün-Regel (Abschnitt 6). TDD-Anker oder Judge nutzen. |
| `forge doctor`: `ANTHROPIC_API_KEY not set` | Kein Fehler, wenn `claude /login` aktiv ist — Subscription-Auth reicht. |
| `merge_pr`-Capability wird abgelehnt | Beabsichtigt — hartkodiert aus. Merge ist Operator-Sache. |
| Generation wird als `guardrail_blocked` abgebrochen | Der Diff berührte eine `forbidden`-Zone oder eine nicht freigegebene Aktion. |
| Run endet sofort `cost_cap_hit` | `cost_caps` zu eng — `per_run_usd` erhöhen. |
| Eval-Gate immer rot | Eval-`cmd` produziert nicht das vom `parses`-Parser erwartete Format. |
