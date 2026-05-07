# forge — Spezifikation v0.3

> Status: Working Draft v0.3
> Autor: Rudolf
> Letzte Änderung: 2026-05-07
> Vorgänger: [`forge-spec-v0.2.md`](forge-spec-v0.2.md) — bleibt als historisches Dokument im Repo
> Plan: [`spec-v0.3-plan.md`](spec-v0.3-plan.md) — Designentscheidungen, die v0.3 motivieren

> **Änderungen gegenüber v0.2** (zusammengefasst): Multi-Agent-Schicht
> (architect/developer/tester als Claude-Code-Subagents) nativ verankert.
> Plan-First-Workflow als first-class — Plan ist Markdown-Artefakt im
> Blob-Store mit eigenem Event-Kind. Inter-Generation-Memory: KEPT-Diffs
> werden in den Prompt der nächsten Generation gespliced. Score-Logic
> präzisiert: drei explizite Modi (Gate-Revival, Composite-Optimization,
> Trade-off in v2). venv-Auto-Detection als Pflicht-Feature. cmd-
> Normalisierung als Cross-platform-Standard. base_commit-Tracking
> über KEEP-Generations hinweg. Trigger-spezifische Subagent-Listen.
> Subagent-Markdowns liegen Hybrid: forge-Defaults plus per-Projekt-
> Override. PINTA-Lessons-Learned dokumentiert.

---

## Teil 1 — Vision

**forge ist in v1 eine messbare, replay-fähige Auto-PR-Maschine.** Daraus
entsteht — wenn die Maschine zuverlässig läuft und genug Daten gesammelt
sind — eine Software-Fabrik. Aber nicht umgekehrt. Die Fabrik ist das
Ergebnis disziplinierter Iteration auf einer engen, prüfbaren Basis. Sie
ist nicht die Ausgangsannahme.

forge ist eine **Telemetrie-Hülle und ein Spec-Wächter** um Claude-Code-
Sessions, plus Trigger-Layer und PR-Erstellung. Den Coding-Loop selbst
implementiert forge nicht — Claude Code orchestriert intern Subagents
(architect, developer, tester) via `Task`-Tool. forges Stärke ist nicht
„Code schreiben", sondern **„Code-Änderungen messen, vergleichen,
dokumentieren, in PRs gießen"**.

Der eigentliche Wert von forge ist nicht „Agent schreibt Code". Den Wert
liefern bereits viele andere Tools. Der Wert ist: **Agenten werden wie
Produktionsprozesse gemessen, verglichen und verbessert.** Daraus folgt,
dass die ersten 100 Stunden Arbeit am System nicht in Strategie-Vielfalt
fließen, sondern in saubere Telemetrie und harte Guardrails.

Der Mensch ist Operator. Er definiert Ziele, Constraints und Erfolgs-
kriterien — neuerdings auch durch **Plan-Approval**: der architect-Subagent
produziert einen Plan als Markdown, der Operator kann ihn reviewen, bevor
Code geschrieben wird. Auto-Merge ist in v1 weiterhin kategorisch
ausgeschlossen.

---

## Teil 2 — Drei Prinzipien (unverändert seit v0.2)

### Prinzip 1 — Trennung von Messbarkeit und Optimierungsbefugnis

> Alles, was autonom optimiert werden darf, muss messbar sein.
> Nicht alles Wertvolle darf autonom optimiert werden.

Die zweite Hälfte ist die wichtige. Architektonische Eleganz, Domain-
Logik-Klarheit, langfristige Wartbarkeit, API-Designqualität bleiben
menschliche Verantwortung. Die Fabrik fasst sie nicht an.

### Prinzip 2 — Telemetrie ist die Basis, nicht das Reporting

Jeder Schritt jeder Iteration produziert ein typisiertes, immutables Event
mit referenzierten Artefakten. Ohne lückenlose Events gibt es keine
spätere Selbstverbesserung — nur Glaube an die eigene Anekdote.

### Prinzip 3 — Strikte Trennung der Optimierungsebenen

Die Maschine ändert Anwendungs-Code. Loop 2 (später) ändert Strategie-
Auswahl innerhalb eines Runs. Loop 3 (noch später) ändert Defaults der
Maschine — niemals Code der Maschine.

---

## Teil 3 — Architektur in drei Phasen (präzisiert)

```
Phase v1 — Auto-PR-Maschine mit Multi-Agent-Subagents
┌────────────────────────────────────────────────────────────────┐
│ Sequential Strategy: ein Run, optional Multi-Agent-Subagents   │
│ Subagents: architect → developer → tester via Task-Tool        │
│ Plan ist first-class Artefakt mit eigenem Event-Kind           │
│ Operator analysiert Events manuell, justiert Spec von Hand     │
└────────────────────────────────────────────────────────────────┘

Phase v2 — Population-Based Search (parallele Strategien)
┌────────────────────────────────────────────────────────────────┐
│ Mehrere parallele Worktrees mit unterschiedlichen Plans/Prompts│
│ LLM-geführter Crossover, Diversity-Penalty, Elitism            │
│ Operator-Analyse weiterhin manuell                             │
└────────────────────────────────────────────────────────────────┘

Phase v3 — Datengetriebene Selbstverbesserung
┌────────────────────────────────────────────────────────────────┐
│ Loop 3: Bandit + BO lernen aus Events, schreiben Defaults      │
│ Strategie-, Subagent- und Prompt-Wahl wird datengetrieben      │
│ Shadow-Mode + Replay-Tests + Signing                           │
└────────────────────────────────────────────────────────────────┘
```

**Wording-Anpassung gegenüber v0.2:** Multi-Agent-Subagents sind Teil
von v1, nicht v2. Population-Based-Search in v2 = parallele Generations
mit *unterschiedlichen* Plans/Prompts, nicht nur „Strategievielfalt".

**Übergangskriterium v1 → v2:** Mindestens 100 abgeschlossene Runs im
Event-Store, dokumentierte Beobachtung mehrerer Plateaus, die mit
sequentieller Strategie nicht überwunden werden konnten.

**Übergangskriterium v2 → v3:** Mindestens 300 abgeschlossene Runs
mit Population-Strategie, Replay-Test-Infrastruktur steht, signierte
factory_state-Auslieferung ist getestet.

---

## Teil 4 — Die Currency: Events (erweitert)

### 4.1 Event-Schema

Unverändert gegenüber v0.2 (siehe dort für vollständige Pydantic-Definition).
Drei Garantien bleiben:

- **`payload_schema_version` pro Kind** — additive Schema-Evolution möglich,
  breaking changes verboten
- **Artefakte als CAS-References** — Inhalt im Blob-Store, Event hält nur
  `sha256:`-Hash
- **Tool-Versionen sind Teil des Replay-Kontrakts**

### 4.2 Event-Kinds — v0.3 erweitert (17+ Kinds)

| Kind | Wann | v0.3-Status |
|---|---|---|
| `RunStarted` | Loop-Start | unverändert |
| `RunFinished` | Loop-Ende | unverändert |
| `GenerationStarted` | vor Iteration | unverändert |
| `GenerationFinished` | nach Iteration | unverändert |
| `PlanProposed` | nach architect-Subagent | **NEU in v0.3** |
| `ProposalRequested` | vor Coding-Agent-Call | unverändert |
| `ProposalReceived` | nach Coding-Agent-Call | unverändert |
| `MutationApplied` | nach Patch | unverändert |
| `PreflightFailed` | wenn Preflight rot | unverändert |
| `EvalStarted` | vor Eval | unverändert |
| `EvalFinished` | nach Eval | unverändert |
| `DecisionMade` | nach Vergleich | unverändert |
| `PRCreated` | bei Auto-PR | unverändert |
| `PRMerged` | via Webhook | unverändert |
| `PRReverted` | via Webhook/Detection | unverändert |
| `CostCapHit` | bei Überschreitung | unverändert |
| `GuardrailViolation` | bei Verletzung | unverändert |

### 4.3 Neuer Event: `PlanProposed`

Der architect-Subagent (siehe Teil 6.5) produziert pro Generation einen
Plan als Markdown. forge persistiert ihn als CAS-Blob und emittiert
`PlanProposed` mit:

```python
class PlanProposedPayload(BaseModel):
    architect_turns: int
    """Anzahl claude-Turns im architect-Aufruf."""

    subtask_count: int | None
    """Wenn forge den Plan parsen konnte: Anzahl Subtasks. Sonst None."""

    risk_level: Literal["low", "medium", "high", "unknown"]
    """Aus dem ## Risk-Header des Plans extrahiert, falls vorhanden."""

    out_of_scope: list[str] = []
    """Wenn der Plan eine ## Out of scope-Sektion hat, deren Bullets."""
```

Plan-Text selbst liegt als Artefakt unter `artifacts.plan` als CAS-Hash.

### 4.4 Storage / Replay

Unverändert. Replay-Garantie gilt jetzt zusätzlich für Pläne — aus
Event + Blob-Store muss der Plan-Text rekonstruierbar sein.

---

## Teil 5 — Die Projekt-Spezifikation (erweitert)

`.forge/project.yaml` ist der Vertrag zwischen Operator und Maschine.
Drei Erweiterungen gegenüber v0.2:

### 5.1 Trigger-spezifische Subagent-Listen — NEU in v0.3

Pro Trigger kann der Operator entscheiden, welche Subagents involviert sind:

```yaml
triggers:
  on_issue_label:
    auto-fix:
      strategy: sequential
      model: sonnet
      max_iterations: 5
      agents: [architect, developer, tester]   # default für features/fixes
    auto-feature:
      strategy: sequential
      model: opus
      max_iterations: 10
      agents: [architect, developer, tester, reviewer]   # zusätzlich review
      requires_human_review: true
  on_pr_opened:
    strategy: review_only
    model: sonnet
    agents: [reviewer]                          # nur reviewer
  on_ci_failure:
    strategy: sequential
    model: sonnet
    max_iterations: 3
    agents: [developer]                         # eng, kein Plan nötig
```

Default wenn `agents` weggelassen: `[architect, developer, tester]`.

### 5.2 Score-Logic — drei explizite Modi

Spec v0.2 sagte: „Composite-Score ist die zentrale Optimierungsgröße,
Gates sind Pre-Filter." In Praxis ist `scores: []` der Normalfall in der
Smoke-Phase und für `legacy_test_revival`. v0.3 nennt drei Modi explizit:

**Modus 1: Gate-Revival** (rot→grün)

- `scores: []` oder `scores`-werte ohne Veränderung
- Mindestens ein Gate war vorher rot, ist jetzt grün
- **Strikte Bedingung:** alle vorher-grünen Gates müssen grün bleiben.
  Genau ein vorher-rotes muss grün geworden sein.
- KEEP, reason `gate_revival`

**Modus 2: Composite-Optimization** (grün→grün besser)

- Alle Gates vorher grün und nachher grün
- `scores`-Liste ist nicht-leer
- Composite-Wert > Baseline + Tolerance
- KEEP, reason `improvement`

**Modus 3: Trade-off-Resolution** (mehrere Gates rot/grün gemischt)

Out-of-scope für v0.3, kommt in v2 (Pareto-Logik). In v0.3 wird ein
gemischter Übergang (Gate A: rot→grün, Gate B: grün→rot) als DISCARD
mit reason `regression` behandelt — strikte Erhaltung des Status quo.

**Pseudocode** (v0.3-Implementierung):

```python
def keep_or_discard(*, baseline_gate_results, new_gate_results,
                    new_composite, baseline_composite, tolerance, scores_present):
    # Strikte Erhaltung: keine vorher-grüne Gate darf rot werden
    for kind, was_passed in baseline_gate_results.items():
        if was_passed and not new_gate_results.get(kind, False):
            return False, "regression"

    # Modus 1: Gate-Revival
    revived = any(
        not was_passed and new_gate_results.get(kind, False)
        for kind, was_passed in baseline_gate_results.items()
    )
    if revived:
        return True, "gate_revival"

    # Modus 2: Composite-Optimization (nur wenn scores definiert)
    if not scores_present or new_composite is None or baseline_composite is None:
        return False, "no_significant_change"

    delta = new_composite - baseline_composite
    if delta > tolerance:
        return True, "improvement"
    return False, "no_significant_change"
```

### 5.3 Surfaces-Sonderbehandlung für Subagent-Files

`<worktree>/.claude/agents/*.md` werden bei jedem Run frisch installiert
und sind transient. Der Runner darf sie nicht in Commits aufnehmen.
Implementierung: `WorktreeManager.commit(paths=...)` nimmt eine
Whitelist-Path-Liste, der Runner reicht `validation.files_changed` durch
(= nur Surface-Files, die der Mutator erkannt hat).

Spec-Anpassung in `forbidden`-Sektion: optional kann der Operator
`.claude/agents/**` als forbidden listen, um defensive Tiefe zu haben —
aber forge stellt das auch ohne Spec-Eintrag sicher.

### 5.4 capabilities-`run` — Allowlist gilt für Subagents

Wenn `agents: [...]` aktiv ist, gilt die `capabilities.run`-Allowlist
zusätzlich für Subagent-internen Bash-Tool-Aufruf. Der Master-Claude
übersetzt sie via `--allowedTools` an Subagents weiter.

Beispiel: wenn `capabilities.run: ["pytest *", "black *"]`, kann der
developer-Subagent nur pytest und black aufrufen, kein curl, npm, git push.

### 5.5 Cost-Caps

Unverändert. Multi-Agent-Runs sind teurer (Plan + Subagents), Operator
sollte `per_run_usd` höher setzen als Single-Agent-Defaults.

Empfohlene Werte in der Smoke-Phase:

```yaml
cost_caps:
  per_generation_usd: 2.00      # Multi-Agent: höher als 0.50 single-agent
  per_run_usd: 5.00
  per_project_per_day_usd: 20.00
  per_project_per_month_usd: 200.00
```

---

## Teil 6 — Loop 1: Generation Mechanics (erweitert)

Eine Generation ist die kleinste produktive Einheit. v0.3-Phasen:

**Phase 1 — Plan** (NEU in v0.3 als eigene Phase, war vorher in „Propose" implicit)
**Phase 2 — Propose**
**Phase 3 — Mutate**
**Phase 4 — Preflight**
**Phase 5 — Eval**
**Phase 6 — Decide**

### 6.1 Plan (NEU)

Der architect-Subagent wird gerufen (read-only Tools), liest Codebase +
CLAUDE.md + project.yaml, produziert einen Plan als Markdown.

forge:
- extrahiert den Plan-Text aus dem Subagent-Output
- speichert ihn im Blob-Store als `artifacts.plan`
- emittiert `PlanProposed`-Event
- parst (best-effort) Subtask-Anzahl, Risk-Level, Out-of-scope

Wenn der Plan „Insufficient context" zurückgibt, wird die Generation
sofort beendet mit reason `plan_unclear`.

### 6.2 Propose

Der developer-Subagent (oder der Coding-Agent direkt im Single-Agent-Mode)
bekommt den Plan plus Inter-Generation-Memory (siehe 6.7) und
implementiert.

Output ist ein Diff im Worktree (vom Subagent direkt geschrieben, NICHT
strukturiert zurückgegeben).

### 6.3 Mutate (jetzt: Validate)

Da der Subagent Files direkt editiert hat, ist „Mutate" eigentlich
„Validate":

1. Pfad-Check der geänderten Files gegen `surfaces` und `forbidden`
2. Capability-Check der Edit-Aktionen
3. Whitespace + Syntax (per Sprache)

Bei Failure: revert zum aktuellen `_current_base_commit` (siehe 6.5),
GuardrailViolation oder PreflightFailed-Event.

### 6.4 Preflight

Unverändert: surface.guardrails laufen mit Budget <30s. Bei Failure
DISCARD ohne Eval.

### 6.5 Eval

Unverändert. Bei Multi-Agent kann der tester-Subagent vorher die Tests
schon laufen lassen — forges Eval-Phase ist die unabhängige Verifikation.

### 6.6 Decide

Folgt der drei-Modi-Logik aus Teil 5.2.

### 6.7 base_commit-Tracking — NEU in v0.3

Aus dem heutigen Bug:

> Generation 0 KEPT (commit), Generation 1 DISCARD → revert auf
> `worktree.base_commit` blastet KEPT-commit weg.

**Spec-Garantie v0.3:** Nach jeder KEEP-Generation aktualisiert der
Runner einen internen `_current_base_commit` auf den dann committeten
HEAD-SHA. Alle nachfolgenden DISCARD-Reverts gehen auf `_current_base_commit`,
nicht auf `worktree.base_commit`. KEPT-commits sind dauerhaft.

Implementierung: `WorktreeManager.revert(worktree, *, to_commit=None)`
nimmt optional einen Override-Commit; Runner trackt den State.

### 6.8 Inter-Generation-Memory — NEU in v0.3

**Problem:** Gen 0 KEPT eine Mutation. Gen 1 startet, Claude bekommt den
gleichen initial_prompt — weiß nicht, dass Gen 0 schon was gemacht hat.

**Lösung v0.3:** Vor jedem `ProposalRequested` baut der Runner ein
**Run-Context-Artefakt** zusammen, das die akkumulierten KEPT-Diffs
enthält. Es wird als zusätzliches Context-Artefakt mitgegeben, der
Subagent-System-Prompt enthält:

```
## Bereits in diesem Run committed (KEEP)
<diff-zusammenfassung von gen 0..N-1>

Du baust auf diesen Änderungen auf. Suche nach NÄCHSTEN sinnvollen
Mutationen, NICHT Wiederholungen. Wenn du keine sinnvolle weitere
Verbesserung siehst, sage das explizit — der Run wird dann beendet.
```

Damit kann der Run sich selbst beenden, wenn der Subagent meldet
„nichts mehr zu tun" — Cost-Cap-effizient.

### 6.9 Determinismus

Unverändert: nicht erreichbar mit aktuellen LLM-APIs. Mitigationen
(Temperature ≤ 0.3, fixed seed, vollständiges Recording) bleiben
gleich.

---

## Teil 6.5 — Subagents (NEU in v0.3)

forge nutzt Claude Code's eigene Subagent-Mechanik (`.claude/agents/<name>.md`),
nicht eine eigene Multi-Agent-Implementierung. Begründung in
[`spec-v0.3-plan.md`](spec-v0.3-plan.md): Claude Code hat Plan-Mode,
Tool-Allowlists, hooks, Skills — alles bereits produktiv. Eigene
Multi-Agent-Logik wäre 2000+ Zeilen Code für ersetzbare Funktionalität.

### Pflicht-Subagents in v1

| Name | Tools | Output |
|---|---|---|
| `architect` | Read, Glob, Grep | Plan-Markdown |
| `developer` | Read, Edit, Write, Bash | Diff im Worktree (kein Text-Return) |
| `tester` | Read, Edit (nur tests/), Write (nur tests/), Bash | Test-Status-Report |

### Optional in v1.5+

| Name | Tools | Output |
|---|---|---|
| `reviewer` | Read, Glob, Grep | Approval/Rejection mit Begründung |
| `operations` | Read, lokale Telemetrie-Tools | Issue-Vorschläge |

### Wo die Subagent-Markdowns liegen — Hybrid (Designentscheidung 5.1)

- **Default-Templates** in `forge-execute/agents/templates/<name>.md`
- **Per-Projekt-Override** unter `<project>/.forge/agents/<name>.md`
- Beim Run wird `.forge/agents/`-Verzeichnis (falls vorhanden) zuerst
  in `<worktree>/.claude/agents/` kopiert, dann nicht-vorhandene Subagents
  aus den forge-Defaults ergänzt. Override hat Vorrang.

### Plan-Format — Markdown mit Standard-Sektionen

Designentscheidung 5.2: strukturiert, aber LLM darf abweichen. Erwartet
sind die Sektionen:

```markdown
# Plan: <Kurztitel>

## Goal
<1-2 Sätze>

## Acceptance
<wie verifizieren>

## Existing patterns I found
<Bullets, mit Datei:Zeile-Referenzen>

## Design decisions
<Numerierte Entscheidungen mit Begründung>

## Subtasks
1. **<Titel>** — file: `<path>` — change: `<2-3 Sätze>` — verified by: `<test>`
2. ...

## Out of scope
<Bullets>

## Risk
<low / medium / high mit Begründung>
```

forge parst best-effort:
- Subtask-Count = Anzahl numerierter Items in `## Subtasks`
- Risk-Level = erstes Wort in `## Risk` (low/medium/high/unknown)
- Out-of-scope = Bullets in `## Out of scope`

Wenn Sektionen fehlen, parsen wir was da ist, der Plan bleibt trotzdem
gültig (LLM darf abweichen).

### Subagent → forge: wie der Plan zurückkommt

Der Master-Claude (= forge's claude-Aufruf) ruft architect via Task-Tool.
Subagent's Antwort kommt als Tool-Result zurück. forge extrahiert den
Plan via:

1. Stop_reason == `end_turn` (Subagent fertig)
2. Letztes Tool-Result-Block mit type `text` enthält den Plan
3. Wenn der Block mit `# Plan:` beginnt → das ist der Plan

forge speichert den Plan-Text als CAS-Blob und emittiert `PlanProposed`.
Wenn das Format nicht erkennbar ist (z.B. „Insufficient context" zurück),
wird das im Event-Payload mit `subtask_count=None` markiert und die
Generation als `plan_unclear` beendet.

### Tool-Allowlists — universal vs project-spezifisch

- Subagent-Tool-Liste (Read, Edit, Write, etc.) ist **universal** und
  steht in der Subagent-Markdown-Frontmatter.
- **Project-spezifische Bash-Patterns** (welche Befehle dürfen
  ausgeführt werden) kommen aus `capabilities.run` der project.yaml und
  werden vom Master-Claude an die Subagents via `--allowedTools`
  weitergereicht.

Begründung: Subagent-Rolle ist universal (architect ist immer read-only,
egal welches Projekt). Nur die Bash-Allowlist hängt vom Stack ab.

---

## Teil 7 — Sicherheit & Guardrails (erweitert)

### 7.1 Schichtung der Guardrails

**Vier Schichten + venv-Auto-Detection:**

1. Forbidden Zones (Pfad-Ebene)
2. Capabilities (Aktions-Ebene)
3. Cost-Caps (Ressourcen-Ebene)
4. Sandbox-Isolation (Prozess-Ebene)
5. **venv-Auto-Detection** (NEU in v0.3) — Subprozesse laufen in der
   Projekt-venv, nicht im System-Python (Spec 7.6)

### 7.2-7.5 (unverändert)

Egress-Kontrolle, Prompt-Injection-Defense, Forge-optimiert-nicht-sich-
selbst, Auto-Merge kategorisch ausgeschlossen — alles wie v0.2.

### 7.6 venv-Auto-Detection (NEU in v0.3)

**Problem (heute beobachtet):** PINTAs Eval-Subprocess fand 0 Tests, weil
`python` aus dem System-PATH genommen wurde, nicht aus der Projekt-venv.
PINTAs sqlalchemy & co waren nicht im System-Python installiert →
ImportError → Tests gar nicht collected.

**Lösung:** Bei jedem Subprocess-Aufruf (Eval, Preflight, claude) sucht
forge aufwärts vom Worktree nach `.venv/` oder `venv/`. Wenn gefunden:

- `<venv>/Scripts/` (Windows) bzw. `<venv>/bin/` (POSIX) wird vorne an
  PATH geprependet
- `VIRTUAL_ENV` wird gesetzt
- Idempotent: bei wiederholten Aufrufen wird PATH nicht doppelt erweitert

Wenn keine venv gefunden wird, bleibt `env` unangetastet.

**Geltung:** Der Worktree selbst hat keine eigene venv (forge legt sie
nicht an). Die Aufwärts-Suche findet die venv im Repo-Root, was unter
`<repo>/.forge/worktrees/<id>/` der nicht-worktree-übergeordnete Repo
ist.

---

## Teil 8 — Operating Model in v1 (unverändert)

Trigger-Taxonomie, menschliche Rolle, Eskalation — wie v0.2.

---

## Teil 9 — MVP: 11 Punkte (überarbeitet)

Aus v0.2 Teil 9 + v0.3-Erweiterungen:

### M1 — Es läuft (Woche 1-2) — MIT v0.3-Erweiterungen

1. **`.forge/project.yaml`** Loader — Pydantic, mit trigger-spezifischen
   `agents`-Listen (NEU)
2. **Event Store** mit 17 Event-Kinds inkl. `PlanProposed` (NEU)
3. **Sequential Run** mit Plan-Phase als eigener Schritt (NEU)
4. **Worktree + Patch + Revert** mit `_current_base_commit`-Tracking (NEU)
5. **Subagent-Schicht**: 3 Pflicht-Subagents (architect/developer/tester),
   Hybrid-Lookup forge-Defaults + projekt-Override (NEU)
6. **Plan als first-class Artefakt** mit Best-effort-Parser (NEU)
7. **Inter-Generation-Memory** — Run-Context als zusätzliches Artefakt (NEU)
8. **Preflight + Eval** mit venv-Auto-Detection (erweitert)
9. **Gates / Scores / Diagnostics** mit drei explicit Modi (präzisiert)
10. **Cost-Caps** — alle vier Ebenen
11. **PR-Erstellung** mit `gh pr create`
12. **Run Summary** + **`forge analyze`** + **`forge replay`** + **`forge plan`** (NEU)

### Was NICHT in M1 ist

- Population-Based Search
- Bandit / Loop 3
- factory_state, Shadow-Mode
- Modell-Routing
- Container-Sandbox
- Zentrale Aggregation
- reviewer / operations Subagents (kommen in v1.5)

---

## Teil 10 — Manuelle Analysephase (unverändert)

Wöchentliche Routine, ≥100 Runs als Übergangskriterium zu v2.

---

## Teil 11 — Repository-Layout (erweitert)

```
forge/
├── packages/
│   ├── forge-core/                # Schema (jetzt 17 Kinds), Store, Spec, CAS
│   ├── forge-execute/
│   │   ├── src/forge_execute/
│   │   │   ├── runner.py          # Plan-Phase + Inter-Gen-Memory
│   │   │   ├── strategies/sequential.py
│   │   │   ├── mutators/code.py
│   │   │   ├── evaluators/command.py    # venv-aware
│   │   │   ├── _venv.py           # NEU in v0.3
│   │   │   ├── agents/
│   │   │   │   ├── claude_cli.py  # multi_agent-Mode
│   │   │   │   └── templates/     # NEU: architect.md, developer.md, tester.md
│   │   │   ├── gates.py
│   │   │   ├── scoring.py         # 3 Modi
│   │   │   └── worktrees.py       # revert(to_commit=...)
│   │   └── tests/
│   ├── forge-adapters/
│   └── forge-cli/
│       └── src/forge_cli/
│           ├── run.py
│           ├── plan.py            # NEU: forge plan <prompt>
│           ├── analyze.py
│           ├── doctor.py
│           └── replay.py
├── examples/
│   └── pinta/
│       └── .forge/
│           ├── project.yaml
│           └── agents/            # NEU: per-Projekt-Override (optional)
└── pyproject.toml                 # uv-managed monorepo
```

### Per-Projekt-Layout

```
my-project/
├── .forge/
│   ├── project.yaml                # die Spec
│   ├── agents/                     # NEU: optional, per-Projekt-Subagent-Overrides
│   │   ├── architect.md            # überschreibt forge-Default
│   │   └── developer.md
│   ├── events.duckdb               # lokaler Event-Store
│   ├── blobs/                      # CAS, gitignored
│   ├── runs/                       # Run-Summaries (Markdown)
│   └── worktrees/                  # transient, gitignored
└── ... (Projekt-Code)
```

---

## Teil 12 — Lebenszyklus für ein neues Projekt (Lessons aus PINTA)

**Bootstrap-Phase (Tag 0, 1-2 Stunden):**

1. `forge init` im Repo (M2 — kommt nach v0.3-Spec)
2. Spec ausfüllen — die wichtigste Stunde
3. **Reife-Check vor erstem Run** (NEU aus PINTA-Lessons):
   - Saubere conftest, alle Test-Imports auflösbar
   - Eine venv mit allen Deps installiert
   - pytest-Suite mindestens lokal grün ohne forge-Eingriff
4. `forge doctor` — automatischer Check
5. Erstes Smoke `forge run --dry-run` mit Mock-Agent
6. Erstes echtes `forge run` gegen ein bekannt-rotes Issue

**Phase 1: Issue-getrieben** (Woche 1-2) — wie v0.2.

**Phase 2: Scheduled Runs** (Woche 3-4) — wie v0.2.

**Phase 3: Manuelle Analyse** (Monat 2-3) — wie v0.2, plus
Plan-Quality-Review: liest sich die architect-Output-Sammlung wie ein
Operator den Plan auch geschrieben hätte?

**Phase 4 (v2): Population GA**
**Phase 5 (v3): Loop 3**
**Phase 6: Reife** — wie v0.2.

---

## Teil 13 — Skizze: Phasen v2 und v3 (unverändert)

---

## Teil 14 — Lessons Learned aus PINTA-Pilot (NEU in v0.3)

**Erste 5 forge-Runs gegen PINTA produzierten:**
- 1 erfolgreichen PR (`feat(pdf): render company branding`)
- 1 Test-Refactor-PR (`test(quotes): rewrite TestQuotesIntegration`)
- 4 forge-Codebase-Bugs entdeckt + gefixt (Auth-Check, max_turns,
  base_commit-Tracking, cmd-Normalisierung, venv-Detection)
- ~$5 verbrannt in Trial-and-Error

**Wichtige Lessons:**

1. **Multi-Agent ist nicht optional** — Single-Agent-Runs scheiterten
   an Black-Preflight, Subagent-Plan macht sauber lokalisiert was
   nötig ist.

2. **Preflight-Guardrails müssen mit Baseline-State konsistent sein** —
   PINTAs main hat black-Drift, daher kein `black --check` als Preflight
   in der Smoke-Phase.

3. **Eval-Subprocess braucht venv-aware PATH** — System-Python ist
   nicht PINTAs Python.

4. **YAML-Block-Skalare mit `\<newline>` sind Cross-platform-Falle** —
   forge normalisiert jetzt automatisch.

5. **Plan reviewen schlägt fünf rote PRs** — der architect-Subagent
   schreibt einen lesbaren Plan in einer Iteration; manuelles Reviewen
   ist eine Stunde, fünf rote forge-Runs sind ein Tag.

6. **Reife-Anforderungen an Pilot-Projekte** — quarantänierte Tests,
   inkonsistente venv-Strategie, taskforce-Imports gegen falsche
   Versionen: alles Setup-Bugs, die forge nicht fixen kann (außerhalb
   surfaces) und zur Trial-and-Error führen. Pilot-Projekt sollte
   sauber sein, sonst frisst forge Zeit an Setup.

---

## Anhang A — Glossar (erweitert)

| Begriff | Bedeutung |
|---|---|
| **Generation** | Eine Runde Plan→Propose→Mutate→Preflight→Eval→Decide |
| **Run** | N Generations gegen dieselbe Baseline |
| **Plan** | Markdown-Artefakt vom architect-Subagent, first-class CAS-Blob |
| **Subagent** | Claude-Code-Subagent in `.claude/agents/<name>.md` mit Rolle und Tool-Allowlist |
| **Surface** | Klar abgegrenzter Pfad-Bereich |
| **Forbidden** | Pfad-Bereich, in dem forge niemals editiert |
| **Capability** | Aktions-Erlaubnis |
| **Gate** | Hartes Pass/Fail-Kriterium |
| **Score** | Kontinuierliche Metrik |
| **Diagnostic** | Geloggte Metrik, beeinflusst Composite NICHT |
| **Composite Score** | Gewichtete Aggregation, nur wenn Modus 2 (alle Gates grün) |
| **Gate-Revival** | Modus 1: rot→grün-Übergang als KEEP-Trigger |
| **Inter-Generation-Memory** | Akkumulierte KEPT-Diffs als Kontext für nächste Generation |
| **base_commit** | Original-Run-Anker; durch `_current_base_commit` für Revert ersetzt nach KEEP |
| **venv-Auto-Detection** | forge findet `.venv/` aufwärts und prependet in PATH |

---

## Anhang B — PINTA Reference-Spec (v0.3-Stand)

Siehe `examples/pinta/.forge/project.yaml` für die aktuelle, in der Praxis
laufende Spec. Wichtige v0.3-Elemente bereits enthalten:

- `legacy_tests`-Surface für Refactoring der quarantänierten Test-Files
- `legacy_quotes`-Eval-Suite als fokussierte Subset-Suite
- single-line eval-cmd (Cross-platform-robust)
- agents-Liste pro Trigger (kommt mit Implementation)

---

## Anhang C — cmd-Normalisierung

YAML-Block-Skalare (`cmd: |`) erlauben Backslash-Continuations, die auf
POSIX-Shells funktionieren aber auf Windows-cmd.exe nicht. forge
normalisiert deshalb vor Subprocess-Aufruf:

```python
def _normalize_line_continuations(cmd: str) -> str:
    return re.sub(r"[ \t]*\\\r?\n\s*", " ", cmd).strip()
```

Das entfernt `\<newline>`-Sequenzen (mit umgebenden Spaces/Tabs) und
ersetzt sie durch ein einzelnes Space. Cross-platform, idempotent.

---

## Anhang D — Beispielhafter Event-Flow mit Plan (Issue → PR)

```
1. GitHub Webhook (Issue labeled auto-fix)
   forge-adapters/github/webhook.py validiert, startet Run.

2. forge-execute/runner.py:
   Event RunStarted

3. Erste Generation:
   Event GenerationStarted

   # NEU in v0.3: Plan-Phase
   Master-Claude ruft architect-Subagent via Task-Tool.
   Architect liest CLAUDE.md + project.yaml + relevante src/-Files.
   Architect liefert Plan-Markdown zurück.
   Forge speichert Plan als CAS-Blob.

   Event PlanProposed {
     payload: { architect_turns: 12, subtask_count: 3, risk_level: "low",
                out_of_scope: ["ändert Auth nicht"] },
     artifacts: { plan: "sha256:..." }
   }

   Event ProposalRequested  // entspricht: developer-Subagent wird gerufen
   Event ProposalReceived   // developer hat Files editiert
   Event MutationApplied
   Event EvalStarted
   Event EvalFinished
   Event DecisionMade (Modus 1 oder 2)
   Event GenerationFinished

4. Run-Ende: Event RunFinished

5. PR-Erzeugung: Event PRCreated

6. Später: Event PRMerged via Webhook
```

Aus dieser Sequenz lässt sich per Replay vollständig rekonstruieren —
inklusive des Plans, des Diffs, der Eval-Outputs, der Tool-Versionen.

---

## Schluss

forge v0.3 ist **die multi-agent + plan-first Auto-PR-Maschine**. Mehr
nicht. Wenn diese Maschine zuverlässig läuft und über Monate Daten
sammelt, entsteht aus den Daten — über zwei weitere disziplinierte Phasen
— die Software-Fabrik. Wer beim Bauen die Phasen überspringt oder
vermischt, baut keine Fabrik, sondern ein un-debuggbares Agenten-Monster.

Drei Sätze als Mantra (unverändert seit v0.2):

- Nur was messbar ist, darf die Maschine optimieren — und nicht alles
  Wertvolle ist messbar.
- Jeder Schritt ist ein Event. Ohne Events keine Lernkurve.
- Loop berührt seine eigene Loop-Logik nie. Strikte Schichtung ist
  Sicherheit.
