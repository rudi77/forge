# forge — Spezifikation v0.5

> Status: Working Draft v0.5
> Letzte Änderung: 2026-06-04
> Vorgänger: [`forge-spec-v0.4.md`](forge-spec-v0.4.md) — bleibt als
> historisches Dokument im Repo
> Diff-Doku: dieses Dokument beschreibt **nur** die Änderungen gegenüber
> v0.4. Alle Mantras, Prinzipien, Pipeline-Phasen, Kostenebenen,
> Capabilities-Defaults, Event-Felder usw. aus v0.4 gelten unverändert
> weiter, soweit hier nicht explizit etwas umformuliert wird.

> **Änderungen gegenüber v0.4** (zusammengefasst): Neue opt-in
> **Judge-Phase** (Phase 4b) verifiziert den Diff einer Generation gegen
> Akzeptanzkriterien und liefert einen ``llm_judge_score`` ∈ [0, 1].
> ``llm_judge_score`` wird zum **GateKind** (war bereits DiagnosticKind),
> wodurch der Judge die unveränderte keep/discard-Logik bindet. Neuer
> ``judge:``-Konfigblock. Neue read-only Protocol-Methode
> ``CodingAgent.review``. **Kein** neuer EventKind — die Judge-Phase
> nutzt ``EvalMode = "judge"``, das das Schema seit v0.3 vorsieht.

---

## Teil 1 — Motivation

forge v1 bis v0.4 ist eine **„mach-die-Zahl-grün"-Maschine**: die
Decide-Phase behält eine Generation nur, wenn ein messbares Gate von rot
auf grün springt oder der Composite-Score steigt. Das passt perfekt für
Wartungsarbeit mit existierenden Tests (Bug-Fix, Lint, Typen).

Für ein **Feature-Issue**, das ein Mensch in Prosa schreibt, gibt es
aber keine natürliche numerische Metrik. Eine reine Feature-
Implementierung verbessert keinen vorhandenen Score → die Decide-Phase
verwirft sie als ``no_improvement``. forge konnte solche Issues bisher
nur umsetzen, wenn das Issue ausführbare Akzeptanz-Tests mitbrachte
(TDD-Konvention).

Der Judge schließt diese Lücke, **ohne** die Loop-Logik anzufassen
(Mantra 3): er erzeugt einen neuen Messwert, der durch ein gewöhnliches
Gate in die bestehende Maschinerie einfließt.

---

## Teil 2 — Was forge in v1 NICHT tut (unverändert)

Auto-Merge durch forge selbst, Population-Based Search, Bandit/BO,
Self-Improvement — alle vier bleiben kategorisch ausgeschlossen. Der
Judge ändert daran nichts: er **bewertet** nur (read-only), er ändert
weder Code noch Konfiguration, und er kann ein KEEP nur **erlauben**,
nie **erzwingen** (fail-closed, siehe Teil 6).

---

## Teil 3 — Architektur (Diff zu v0.4)

```
forge-core       ── + "llm_judge_score" in GateKind (additive Literal-
                      Erweiterung; war bereits in DiagnosticKind)
                    + JudgeConfig + ProjectSpec.judge
                    + Validator-Warnung: judge.enabled ohne bindendes Gate
forge-execute    ── + CodingAgent.review() + ReviewResult (agents/base)
                    + ClaudeCodeCLIAgent.review (read-only claude -p)
                    + MockCodingAgent.review (static/sequence/callable)
                    + evaluators/judge.py (JudgeEvaluator, fail-closed)
                    + SequentialRunner Phase 4b (opt-in)
forge-cli        ── + forge run --acceptance-file
                    + board-loop reicht Issue-Text als Kriterium durch
                    + forge doctor: _check_judge
```

Die Schicht-Boundaries aus v0.4 gelten unverändert.
``forge_execute.evaluators.judge`` hängt nur an ``forge_execute.agents``;
der Runner orchestriert. Keine zirkulären Imports. ``forge-core`` bleibt
ohne Abhängigkeit zu den anderen Packages.

---

## Teil 4 — Event-Schema (keine Erweiterung)

**Kein neuer EventKind.** Die Judge-Phase emittiert pro Generation ein
zusätzliches ``EVAL_STARTED``/``EVAL_FINISHED``-Paar mit
``eval_mode="judge"`` und ``suite_id="judge"``. Das ``EvalMode``-Literal
``("quick" | "full" | "judge")`` existiert seit v0.3 — der Judge nutzt
nur den bereits vorgesehenen Wert.

- ``EVAL_FINISHED(eval_mode="judge")`` trägt:
  - ``gates_passed`` = (verdict == "pass") — informativ.
  - ``gates = []`` — das **autoritative** Gate-Resultat steht im
    nachfolgenden ``EVAL_FINISHED(eval_mode="quick")``, das alle Gates
    inkl. ``llm_judge_score`` enthält.
  - ``diagnostics = {"llm_judge_score": <score>}``.
  - ``cost_usd`` am Event = Judge-Kosten; ``artifacts.judge_reasoning``
    = Blob mit der Begründung (Replay-Kontrakt).

**Größen-Invariante unverändert:** ``len(EventKind) == 18``,
``len(_PAYLOAD_REGISTRY) == 18``.

---

## Teil 5 — Konfiguration: ``judge:``-Block

Neu in ``ProjectSpec`` (default-konstruiert, also Spec-kompatibel zu
v0.4):

```yaml
judge:
  enabled: false        # Phase 4b Opt-in. Default: aus.
  model: claude-haiku-4-5   # null = Run-Modell. Klein reicht (Bewertung).
  max_turns: 6          # Tool-Turns-Cap für den Judge-Aufruf.
  threshold: 0.8        # Doku-Default für die Gate-Schwelle (s.u.).
  budget_s: 300         # Wallclock-Budget des Judge-Subprozesses.
```

Damit der Judge **wirkt**, braucht es zusätzlich ein bindendes Gate:

```yaml
gates:
  - {kind: llm_judge_score, threshold: 0.8}
```

``forge doctor`` warnt, wenn ``judge.enabled`` gesetzt ist, aber kein
solches Gate existiert — der Judge liefe dann (kostet Geld), ohne die
Entscheidung beeinflussen zu können. Die Spec-Validierung selbst warnt
ebenfalls (kein harter Reject — ein Operator darf den Score absichtlich
nur beobachten wollen).

---

## Teil 6 — Pipeline-Erweiterung: Phase 4b (Judge)

### 6.1 Ablauf

Wenn ``spec.judge.enabled``, läuft pro Generation nach der Eval-Phase
(Phase 4) und **vor** der Gate-Auswertung:

```
Diff der Generation + Akzeptanzkriterien
   → agent.review(...)            (read-only claude -p)
   → ReviewResult{judge_score, verdict, reasoning, cost}
   → measurements["llm_judge_score"] = judge_score
   → evaluate_gates(measurements)  (unverändert)
```

Die Akzeptanzkriterien sind ``RunConfig.acceptance_criteria`` (CLI:
``--acceptance-file``; board-loop: der rohe Issue-Text). Fehlt das Feld,
dient der ``initial_prompt`` als Kriterium.

Das read-only Tool-Set des Judge enthält ``Read``, ``Grep``, ``Glob``
und ``Bash(git diff/log/show/status:*)`` — **kein** ``Edit``/``Write``.
Der Judge ändert nichts.

### 6.2 rot→grün als Trägermechanik

Am Baseline-Stand (vor jeder Mutation) läuft der Judge **nicht** — der
Messwert ``llm_judge_score`` fehlt, das Gate ist damit rot. Nach einer
Generation, die das Feature implementiert und vom Judge bestätigt wird,
ist das Gate grün. Die bestehende ``keep_or_discard``-Logik erkennt das
als ``gate_revival`` und behält die Generation — **ohne** dass ein
numerischer Score sich verbessert haben muss. Genau der Mechanismus, der
seit v0.2 Bug-Fixes behält, trägt jetzt auch Features.

### 6.3 Fail-closed

Scheitert der Judge (claude-Crash, Timeout, unparsbares JSON), liefert
der ``JudgeEvaluator`` ``score = 0.0`` / ``verdict = fail``. Das Gate
bleibt rot → DISCARD. Anders als bei der Triage (die im Zweifel
*durchlässt*, weil ein zu Unrecht geschlossenes Issue teurer ist als ein
unnötiger Run) ist der Judge **fail-closed**: ein nicht-verifizierbarer
Diff wird nie behalten. Der Judge erweitert die Menge der erlaubten
KEEPs nie über das hinaus, was er positiv bestätigt hat.

### 6.4 Kosten

Judge-Kosten zählen wie ein regulärer LLM-Call in die Run-Telemetrie und
die Cost-Caps (``per_run_usd`` / ``per_project_*``). Empfehlung: kleines
Modell (``claude-haiku-4-5``) — Bewertung, kein Code.

---

## Teil 7 — Was sich NICHT geändert hat

- 5-Phasen-Pipeline (Plan → Implement → Validate → Eval → Decide)
  bleibt. Judge ist eine **Sub-Phase (4b)** innerhalb von Eval, keine
  neue Hauptphase und keine Loop-Logik.
- ``scoring.py``, ``gates.py``, ``keep_or_discard`` — **kein Diff**.
- ``payload_schema_version`` aller Kinds bleibt ``"1.0"``.
- Capabilities ``merge_pr``/``push_to_main``/``push_force`` bleiben
  ``Literal[False]``.
- Self-Improvement-Verbot (Prinzip 3) unangetastet — der Judge bewertet
  ausschließlich Target-Repos.

---

## Anhang — Migration v0.4 → v0.5

Bestehende ``.forge/project.yaml``-Specs sind **unverändert gültig**.
Wer den Judge benutzen will, fügt hinzu:

```yaml
judge:
  enabled: true
  model: claude-haiku-4-5
gates:
  - {kind: llm_judge_score, threshold: 0.8}
```

und gibt pro Run Akzeptanzkriterien mit (``forge run
--acceptance-file``) oder lässt ``forge board-loop`` den Issue-Text als
Kriterium durchreichen.

Bestehende Events bleiben lesbar (kein Schema-Break). Replay-Tests
kennen weiterhin die ``len(EventKind) == 18``-Invariante.
