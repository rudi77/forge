# Spec v0.3 — Plan-Dokument

> Status: **Diskussionsentwurf**, noch nicht implementiert. Dieses Dokument
> beschreibt, **was** in Spec v0.3 reinkommt und **warum**, bevor die Spec
> selbst geschrieben wird. Reviewen vor dem Anfassen der Spec.
> Datum: 2026-05-07

## 1. Warum überhaupt v0.3?

Heute (2026-05-06/07) haben wir Multi-Agent-Architektur, Plan-First-Workflow
und mehrere Infrastruktur-Fixes gebaut, die Spec v0.2 nicht abdeckt oder
sogar widerspricht. Konkrete Drift-Punkte:

| Spec v0.2 sagt | Realität |
|---|---|
| „Claude Code CLI als Plug-in, ein Coding-Agent" | architect/developer/tester als Subagents, claude wird zur Orchestrationsplattform |
| „16 Event-Kinds" | brauchen 17+ (PlanProposed; evtl. SubtaskCompleted) |
| „Tests als Spec, Eval entscheidet" | Plan ist die echte Spec; Tests sind Akzeptanz-Kontrakte |
| „Score-Composite ist die Optimierungsgröße" | scores=[] ist heute der Normalfall — KEEP triggert auf Gate-Übergänge, nicht Score-Deltas |
| nichts zu venv | venv-Auto-Detection ist Kernfunktion |
| nichts zu cmd-Robustheit | Cross-platform-cmd-Normalisierung implementiert |

Spec v0.2 wird **nicht ungültig** — aber sie ist unvollständig und an einigen
Stellen irreführend. v0.3 ist additiv: alles aus v0.2 bleibt, manches wird
präzisiert, neues kommt dazu.

## 2. Was in v0.3 NEU rein muss

### 2.1 Multi-Agent-Schicht (neue Spec-Sektion „Teil 6.5: Subagents")

Designentscheidung: forge ist ein **Container** um Claude-Code-Sessions, die
intern Subagents orchestrieren. forge selbst implementiert keinen
Multi-Agent-Loop — Claude Code tut das via `Task`-Tool.

**Pflicht-Subagents (in v1):**

- **architect** — read-only, produziert Plan als Markdown
- **developer** — implementiert genau einen Subtask aus Plan
- **tester** — schreibt/läuft Tests, verifiziert Akzeptanz

**Optional (v1.5+):**

- **reviewer** — prüft Diff gegen Plan + CLAUDE.md
- **operations** — beobachtet Run-Telemetrie, schlägt Issues vor

**Wo Subagents leben:**

- Default-Templates in `forge-execute/agents/templates/*.md`
- Projekt-Override unter `<project>/.forge/agents/*.md` (nimmt Vorrang)
- Beim Run werden sie in `<worktree>/.claude/agents/` kopiert

**Tool-Allowlists pro Subagent** sind Teil der Subagent-Markdown-Frontmatter,
nicht der project.yaml. Begründung: Subagents sind universal, ihre Tools
ergeben sich aus der Rolle (architect ist immer read-only, egal welches
Projekt). Project-spezifisch ist nur **welche Bash-Patterns** erlaubt sind —
die kommen aus `capabilities.run` der project.yaml.

### 2.2 Plan-First-Workflow (neue Spec-Sektion „Teil 6.6: Plans")

Plan ist ein **first-class Artefakt**:

- Wird vom architect-Subagent als Markdown produziert
- Wird im Blob-Store persistiert (CAS-Hash)
- Wird über neuen Event-Kind `PlanProposed` referenziert
- `forge replay <run_id>` zeigt Plan separat, nicht nur eingebettet im Subagent-Trace

**Plan-Schema** (informell, kein YAML):

```markdown
# Plan: <Kurztitel>

## Goal
## Acceptance
## Existing patterns I found
## Design decisions
## Subtasks
1. **<Titel>** — file: `<path>` — change: `<2-3 Sätze>` — verified by: `<test>`
## Out of scope
## Risk
```

Begründung freie Markdown statt YAML: LLM trifft Markdown-Format zuverlässiger,
und der Plan ist primär für menschliche Reviewer da — nicht für maschinelle
Validierung.

**Lifecycle:**

1. `forge plan <prompt>` — generiert nur den Plan, kein Code (M1.5 — neuer
   CLI-Subcommand)
2. Operator reviewt, akzeptiert ggf. mit Änderungen
3. `forge run --plan <plan-file>` — führt einen externen Plan aus (M2)
4. Default `forge run` (ohne `--plan`): architect erzeugt Plan inline,
   forge persistiert ihn, dann developer

### 2.3 Inter-Generation-Memory (neue Spec-Sektion „Teil 6.7")

**Problem (heute beobachtet):** Gen 0 KEPT eine Mutation. Gen 1 bekommt den
gleichen initial_prompt — Claude weiß nicht, dass Gen 0 schon was gemacht
hat → schlägt eine Variation vor → keine messbare Verbesserung → DISCARD.
Geld verbrannt.

**Lösung:** Wenn Gen N+1 startet, wird der akkumulierte Diff aller bisher
KEPT-Generations als **Kontext-Artefakt** in den Prompt gespliced:

```
<Original-Prompt>

## Bereits in diesem Run committed (KEEP)
<diff von Gen 0..N-1>

Du baust auf diesen Änderungen auf. Suche nach NÄCHSTEN sinnvollen
Mutationen, NICHT Wiederholungen.
```

forge-core: neuer „Run-Context"-Helper, der pro Generation die akkumulierten
KEEPs zusammenfasst. Der Runner reicht den als zusätzliches Context-Artefakt
in `ProposalRequested` ein.

### 2.4 venv-Auto-Detection (neue Spec-Sektion „Teil 7.6")

forge sucht aufwärts vom Worktree nach `.venv/` oder `venv/`. Wenn gefunden:

- `<venv>/Scripts/` (Windows) bzw. `<venv>/bin/` (POSIX) wird vorne an PATH
  geprependet
- `VIRTUAL_ENV` wird gesetzt
- Gilt für: Eval-Subprocess, Preflight-Subprocess, claude-Subprocess

Cross-platform, idempotent. Wirksam in allen 3 Subprocess-Aufrufen.

### 2.5 Cmd-Normalisierung (Anhang)

YAML-Block-Skalare (`cmd: |`) mit `\<newline>`-Continuations sind
POSIX-Shell-Konvention. Cmd.exe ignoriert sie — nackt durchgereicht ist
das `\` ein Path-Separator-Bug (Drive-Root-Crash).

forge normalisiert vor `subprocess.run`: `re.sub(r'[ \t]*\\\r?\n\s*', ' ', cmd).strip()`

### 2.6 base_commit-Tracking (Klarstellung in Teil 6.5)

Aktualisiert nach jedem KEEP. Sonst hauen DISCARD-Generations die KEPT-Commits
weg. Spec v0.2 sagt nichts dazu.

## 3. Was in v0.3 PRÄZISIERT wird

### 3.1 Score-Logic — die wichtigste Designdiskussion

**Spec v0.2:** „Composite-Score ist die zentrale Optimierungsgröße. Gates
sind Pre-Filter, dann wird der gewichtete Score-Composite verglichen."

**Realität:** scores=[] ist der Normalfall in Smoke- und legacy_test_revival-
Phase. KEEP triggert ausschließlich auf Gate-Übergängen (rot→grün).

**Frage:** Ist das ein Bug oder ein Feature?

**Antwort v0.3:** Feature, mit Klarstellung:

- **Gates ersetzen Composite, wenn scores leer sind** — KEEP-Logic prüft
  zuerst Gate-Übergang (was vorher rot war ist jetzt grün), dann Composite-
  Delta nur wenn alle Gates schon vorher grün waren.
- Spec v0.3 nennt drei Optimierungs-Modi explizit:
  1. **Gate-Revival**: bei rot→grün-Übergang KEEP, scores irrelevant.
     (Der heutige PINTA-Sweet-Spot.)
  2. **Composite-Optimization**: bei grün→grün KEEP nur wenn Composite
     besser. Klassischer „Optimierungslauf" wie in Spec v0.2 beschrieben.
  3. **Trade-off-Resolution** (zukünftig v2): bei mehreren Gates und
     Scores entscheidet eine Pareto-Logik. Out-of-scope für v0.3.

### 3.2 CodingAgent-Protocol vs Multi-Agent

**Spec v0.2:** „Claude Code CLI als Plug-in, das `CodingAgent`-Protocol
hat propose / review / estimate_cost."

**Realität:** Das Protocol bleibt, aber `ClaudeCodeCLIAgent` hat einen
`multi_agent`-Mode, der intern keine Subagents implementiert sondern
Claude Code's eigene Subagent-Mechanik (Task-Tool, .claude/agents/) nutzt.

**Klarstellung v0.3:** Das `CodingAgent`-Protocol ist die forge-seitige
Abstraktion (austauschbar für Codex, OpenCode etc.). Multi-Agent ist eine
**Eigenschaft der konkreten Implementation**, nicht des Protocols. Andere
Agents können andere Multi-Agent-Mechaniken haben.

### 3.3 Forbidden / Surfaces — Subagent-Files

`<worktree>/.claude/agents/*.md` werden bei jedem Run frisch installiert
und sind transient. Der Runner darf sie NICHT in Commits aufnehmen.
WorktreeManager.commit nimmt nur eine Whitelist-Path-Liste — nicht
`git add -A`.

Klarstellung in v0.3 Teil 5.

## 4. Was in v0.3 RAUS oder DEPRIORISIERT wird

### 4.1 „Loop 2 / Loop 3" als formelle Phasenstruktur — bleibt, aber...

Spec v0.2 Teil 3 zeigt die drei Phasen als strikte Abfolge:
1. v1 — Auto-PR
2. v2 — Population-Based Search
3. v3 — Loop 3 (Bandit, BO)

**v0.3-Reflexion:** Das Multi-Agent-Modell hat die „Strategievielfalt" aus
v2 partiell vorgezogen — verschiedene Subagents sind eine Art von
Strategievielfalt innerhalb eines Runs. Population-Based-Search bleibt
ein eigenständiges v2-Thema, aber die Begründung „v1 hat keine
Strategievielfalt" stimmt nicht mehr.

**Wording-Anpassung:** v2 = parallele Generations mit Crossover, statt
„v1 hat nur eine Strategie".

### 4.2 „Erster Reference-Projekt PINTA" — Lessons Learned

Spec v0.2 Teil 14: „PINTA als Pilot." Heute Realität: PINTA hat
- legacy class-based Tests die quarantäniert sind
- pytaskforce-Drift bei Imports (mittlerweile gelöst)
- Inkonsistente venv-Annahmen
- conftest-Lücken

→ v0.3 dokumentiert: ein „Pilot-Projekt" muss bestimmte **Reife** haben:
  saubere conftest, deterministische Tests, eine venv mit allen Deps.
  Sonst frisst forge an Setup-Bugs herum.

## 5. Offene Designfragen — vor v0.3 zu klären

### 5.1 Wo lebt die Subagent-Konfiguration?

**A:** Komplett in forge selbst — alle Projekte nutzen die Default-
Subagents. Konsistenz, aber kein Project-Customization.

**B:** Per-Projekt in `<project>/.forge/agents/`. Maximaler Customization,
aber jeder Operator muss das pflegen.

**C:** Hybrid — forge liefert Defaults, Projekt kann per-File überschreiben.

**Heutige Implementation:** A (nur in forge-execute templates). Aber
ich tendiere zu C als v0.3-Standard. Klärung mit Operator.

### 5.2 Plan-Format — Markdown-frei oder Markdown-Schema?

**A:** Frei (heutige Implementation) — LLM trifft natürlicher.

**B:** Strukturierte Sektionen mit harten Headers (## Goal, ## Subtasks etc.) —
forge kann den Plan parsen, einzelne Subtasks als Events emittieren.

**Trade-off:** A ist robust, B ist informativ. v0.3 sollte B als
Erweiterung von A definieren — Sektionen sind „erwartet aber nicht
hart gefordert".

### 5.3 Trigger-Strategy für Subagents

Heute ist `--multi-agent` ein flag. Sollte es Trigger-spezifisch sein?

- `on_issue_label.auto-fix` → multi-agent default
- `on_ci_failure` → single-agent (CI-Fix ist eng, ein Subagent reicht)
- `schedule.legacy_test_revival` → multi-agent default
- `on_pr_opened.review_only` → reviewer-Subagent, kein architect/developer

v0.3 sollte triggers in der project.yaml einen `agents`-Block haben mit
expliziter Subagent-Liste pro Trigger.

### 5.4 Gate-Übergangs-Erkennung als KEEP-Trigger

Heute (in `scoring.keep_or_discard`):
```python
if not baseline_gates_passed and gates_passed:
    return True, "improvement"
```

Das ist boolean — ein einziger Gate-Übergang. In Realität haben Specs
mehrere Gates (pytest, ruff, mypy, ...).

**Question:** Wenn Gate A rot war + jetzt grün, aber Gate B grün war +
jetzt grün → KEEP? Wenn Gate A rot war + jetzt grün, aber Gate B vorher
grün + jetzt rot → DISCARD?

v0.3-Vorschlag: KEEP nur wenn **strikt mehr Gates grün** sind als vorher.
Alle vorher-grünen müssen grün bleiben.

## 6. Reihenfolge der Spec-Anpassung (im Spec-File selbst)

Damit Spec-Diff klein bleibt:

1. **Header** — Status auf v0.3, Datum, Änderungs-Liste gegen v0.2
2. **Teil 6 erweitern** mit 6.5 (Subagents), 6.6 (Plans), 6.7 (Inter-Gen-Memory)
3. **Teil 7.6** für venv-Auto-Detection
4. **Teil 5.1** Score-Logic-Klarstellung (3 Modi)
5. **Anhang** für cmd-Normalisierung
6. **Teil 14** Lessons Learned aus PINTA-Pilot

Insgesamt ~600 neue Zeilen in der Spec, vermutlich ~150 Zeilen
modifiziert. Spec wird ~50% länger.

## 7. Implementation-Roadmap nach Spec-Approval

| # | Task | Aufwand | Adressiert |
|---|---|---|---|
| 1 | `PlanProposed`-EventKind in forge-core | 3-4 Std | 2.2 |
| 2 | Plan als Artefakt — claude_cli.py extrahiert architect-Output, ruft store.append() | 4-6 Std | 2.2 |
| 3 | `forge replay <id>` Plan separat anzeigen | 2 Std | 2.2 |
| 4 | Inter-Generation-Memory — Run-Context-Helper, im Runner spliced | 3-4 Std | 2.3 |
| 5 | `forge plan <prompt>` CLI-Subcommand (nur Plan, kein Code) | 1 Tag | 2.2 |
| 6 | Score-Logic-Refactor: 3 Modi explizit, Tests | 1 Tag | 3.1 |
| 7 | per-Projekt Subagent-Override (`.forge/agents/`) | 4 Std | 5.1 |
| 8 | Trigger-spezifische Subagent-Liste in project.yaml | 4 Std | 5.3 |
| 9 | Spec v0.3 selbst schreiben | 1 Tag | (alle) |

Total: ~5-6 Tage Programmierung + 1 Tag Spec-Schreiben.

## 8. Akzeptanz für diesen Plan — abgehakt 2026-05-07

- [x] **5.1** — Hybrid: forge-Defaults + per-Projekt-Override unter
  `<project>/.forge/agents/<name>.md` mit Vorrang
- [x] **5.2** — Strukturiert mit Standard-Sektionen, LLM darf abweichen
  (Sektionen werden vom architect "erwartet, aber nicht hart gefordert")
- [x] **5.3** — Trigger-spezifische Subagent-Listen in project.yaml
- [x] **5.4** — Strikte Gate-Übergangs-Logik: KEEP nur wenn alle vorher-grünen
  grün bleiben UND mindestens ein vorher-rotes jetzt grün ist
- [x] Spec-Anpassungs-Reihenfolge ok
- [x] Implementation-Roadmap-Reihenfolge ok

→ Spec v0.3 wird als nächstes geschrieben (`docs/forge-spec-v0.3.md`).
