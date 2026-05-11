# forge — Spezifikation v0.4

> Status: Working Draft v0.4
> Autor: Rudolf
> Letzte Änderung: 2026-05-11
> Vorgänger: [`forge-spec-v0.3.md`](forge-spec-v0.3.md) — bleibt als
> historisches Dokument im Repo
> Diff-Doku: dieses Dokument beschreibt **nur** die Änderungen gegenüber
> v0.3. Alle Mantras, Prinzipien, Pipeline-Phasen, Kostenebenen,
> Capabilities-Defaults, Event-Felder usw. aus v0.3 gelten unverändert
> weiter, soweit hier nicht explizit etwas umformuliert wird.

> **Änderungen gegenüber v0.3** (zusammengefasst): GitHub-Project-Boards
> sind aktive Trigger-Quelle für ``forge board-loop``. ``--auto-merge``
> als opt-in-Flag pro Aufruf, ohne ``merge_pr``-Capability zu brechen.
> Neue Pre-Phase **Issue-Triage** mit eigenem Event-Kind
> ``IssueTriaged`` und neuen Capabilities ``comment_issue`` /
> ``close_issue``. PR-Body rendert den ``PlanProposed``-Markdown
> mit. Worktree-GC + Branch-Cleanup laufen am Loop-Start.
> Subprocess-Cleanup nach Auto-Merge ist von forge **nicht** Aufgabe —
> GitHub-Seite (``--delete-branch``).

---

## Teil 1 — Vision (unverändert)

Siehe v0.3 Teil 1. forge bleibt in v1 die messbare, replay-fähige
Auto-PR-Maschine. v0.4 erweitert ausschließlich die **Trigger-Quelle**
(Board statt nur Webhook), das **Vorab-Filtering** (Triage statt
blinder Dispatch) und einen **operativen Komfort-Layer**
(Auto-Merge-Queue, GC, Plan-in-PR-Body). Keine neuen
Optimierungsebenen, kein Self-Improvement, kein Auto-Merge durch forge
selbst — die drei Prinzipien aus v0.2/v0.3 sind weiterhin Vertrag.

---

## Teil 2 — Was forge in v1 NICHT tut (kategorisch, unverändert)

- Auto-Merge durch forge selbst (``capabilities.merge_pr``,
  ``push_to_main``, ``push_force`` bleiben ``Literal[False]``).
- Population-Based Search (Loop 2 in v2).
- Bandit / Bayesian Optimization (Loop 3 in v3).
- Self-Improvement (Prinzip 3).

**Klarstellung zu Auto-Merge:** ``forge board-loop --auto-merge`` und
``forge run --auto-merge`` rufen ``gh pr merge --auto --squash
--delete-branch`` auf. Das **queued** den Merge auf GitHub-Seite;
der eigentliche Merge passiert server-seitig durch GitHubs Bots,
asynchron, sobald alle required Checks grün sind. forge selbst führt
**keinen** ``merge``-Subprozess aus. Die ``merge_pr``-Capability
verbietet weiterhin synchrones Mergen durch forge; sie sagt nichts
über GitHub-Server-Features aus, die ein Operator anfordert. Wer das
nicht will, lässt ``--auto-merge`` weg — Default bleibt aus.

---

## Teil 3 — Architektur (Diff zu v0.3)

```
forge-core       ── + EventKind.ISSUE_TRIAGED, + IssueTriagedPayload,
                    + TriageConfig, + CapabilitiesConfig.comment_issue,
                    + CapabilitiesConfig.close_issue
forge-execute    ── + forge_execute/triage/ (LLMTriager, NoopTriager,
                    gh-Helpers comment_issue/close_issue)
                    + WorktreeManager.gc_stale, .prune_merged_branches,
                      .list_forge_worktrees
forge-cli        ── + board_loop._run_triage, ._emit_triage_event,
                      ._build_triager, ._run_garbage_collection
                    + execute_run lädt PlanProposed-Markdown aus dem
                      Blob-Store und reicht ihn an render_pr_body durch
forge-adapters   ── + render_pr_body(plan_md=...) Section
```

Die Schicht-Boundaries aus v0.3 gelten unverändert. ``forge_execute.triage``
hängt an ``forge_core.events`` (für EventKind) und ``forge_adapters.github``
(für ``ReadyIssue`` als Eingabe); ``forge_cli.board_loop`` orchestriert
beide. Keine zirkulären Imports.

---

## Teil 4 — Event-Schema-Erweiterung

### 4.1 Neuer Kind ``IssueTriaged``

Pro Issue, das der board-loop sieht, emittiert die Triage-Phase
**genau ein** ``IssueTriaged``-Event — auch wenn das Triage-Ergebnis
``relevant`` ist (dann läuft der Dispatch zusätzlich, mit eigener
``run_id``). Korrelations-Anker zwischen Triage und Dispatch ist
``payload.issue_number``.

Payload (``payload_schema_version`` ``"1.0"``):

| Feld              | Typ                                                                   | Beschreibung                                                       |
|-------------------|-----------------------------------------------------------------------|--------------------------------------------------------------------|
| ``issue_number``  | ``int`` (``>0``)                                                      | GitHub-Issue-Nummer.                                               |
| ``decision``      | ``Literal["relevant","stale","duplicate","already_solved"]``          | Klassifikat.                                                        |
| ``reason``        | ``str``, max 2000 Zeichen                                             | Kurze textuelle Begründung; bei ``auto_comment`` als Kommentar.    |
| ``related_pr``    | ``int \| None``                                                       | Bei ``duplicate`` die PR/Issue-Nummer.                              |
| ``related_commit``| ``str \| None``                                                       | Bei ``already_solved`` der Short-SHA.                               |
| ``turns_used``    | ``int``                                                               | Claude-Tool-Turns im Triage-Aufruf.                                 |

Das Event-Top-Level-Feld ``cost_usd`` trägt die Triage-Kosten; ``model``
das verwendete Claude-Modell. Damit ist die Triage-Ökonomie pro Issue
replay- und auswertbar (z.B. „kostet Triage weniger als der durch sie
vermiedene Dispatch?").

**Größen-Invariante:** ``len(EventKind) == 18``, ``len(_PAYLOAD_REGISTRY) == 18``.

### 4.2 Bestehende Kinds (unverändert)

Alle 17 Kinds aus v0.3 (siehe ``forge-spec-v0.3.md`` Teil 4.2) gelten
weiter mit ``payload_schema_version`` ``"1.0"``. Keine breaking changes.

---

## Teil 5 — Capabilities-Erweiterung

``CapabilitiesConfig`` bekommt zwei neue boolean Felder, beide
defaultmäßig ``True``:

```python
comment_issue: bool = True   # gh issue comment
close_issue:   bool = True   # gh issue close
```

Begründung der Defaults: ein Operator, der ``triage.enabled = True``
opt-in setzt, will praktisch immer auch, dass Kommentar+Close
funktionieren — sonst wäre Triage zahnlos. Wer das engmaschiger will,
setzt eines oder beides explizit auf ``False``.

``CapabilityAction`` wird um ``"comment_issue"`` und ``"close_issue"``
erweitert. ``Capabilities.check_action`` evaluiert sie automatisch via
``getattr(spec.capabilities, action)``. Die hart deaktivierten
Aktionen (``merge_pr``, ``push_to_main``, ``push_force``) bleiben
unverändert verboten.

---

## Teil 6 — Pipeline-Erweiterung: ``forge board-loop``

### 6.1 Worktree-GC + Branch-Cleanup (Pre-Loop)

Beim Start von ``forge board-loop`` (default; ``--no-gc`` schaltet es
ab):

1. ``WorktreeManager.gc_stale()`` entfernt alle ``forge/*``-Worktrees,
   die als prunable markiert sind oder deren Verzeichnis fehlt oder
   die im Pool unter ``.forge/worktrees/`` liegen, aber von keinem
   aktiven Run gehalten werden. Worktrees außerhalb des Pools (z.B.
   manuell ausgecheckte ``forge/<id>``-Branches) werden nicht
   angefasst — Operator-Hoheit.

2. ``WorktreeManager.prune_merged_branches()`` führt erst
   ``git fetch --prune origin`` aus und löscht dann lokale
   ``forge/*``-Branches, deren Upstream-Tracking ``[gone]`` ist
   (typisch nach ``gh pr merge --auto --delete-branch``). Branches
   mit aktivem Worktree werden übersprungen.

Beide Schritte sind best-effort: bei Git-Fehlern wird eine Warnung auf
stderr ausgegeben, aber der board-loop bricht nicht ab.

### 6.2 Issue-Listing (unverändert seit v0.4-Init)

``board.list_ready_items()`` zieht aus dem konfigurierten Project,
filtert nach ``filter_status`` + ``filter_labels``, prüft Idempotenz
(skip wenn offener PR mit ``Closes #N`` existiert). Siehe ``BoardConfig``
in v0.3-Folge.

### 6.3 Neu — Issue-Triage als Pre-Phase

Wenn ``spec.triage.enabled == True``, läuft pro Issue **vor**
``execute_run`` ein Triager:

```
ReadyIssue → Triager.triage(issue) → TriageResult
```

Die Default-Impl ist ``LLMTriager``: ein einziger ``claude -p``-Aufruf
mit eng zugeschnittenem Tool-Set (``Read``, ``Bash(gh issue list:*)``,
``Bash(gh pr list:*)``, ``Bash(git log:*)``, ``Bash(git diff:*)``,
``Bash(git show:*)``). Kein ``Edit``/``Write`` — Triage darf nichts
modifizieren. Output ist ein JSON-Block, der gegen
``TriageDecision`` validiert wird:

```json
{
  "decision": "relevant" | "stale" | "duplicate" | "already_solved",
  "reason": "kurze deutsche Begründung",
  "related_pr": 99 | null,
  "related_commit": "abc1234" | null
}
```

Bei ``TriageError`` (claude-Crash, JSON-Garbage, Timeout) fällt der
Triager auf ``decision="relevant"`` zurück. **Triage darf den
Hauptpfad nie blockieren** — im Zweifel wird dispatched.

Pro Issue genau ein ``IssueTriaged``-Event (s. Teil 4.1).

Bei ``decision != "relevant"``:

* Wenn ``triage.auto_comment`` und Capability ``comment_issue``
  erlaubt → ``gh issue comment`` mit der formatierten Begründung
  (deutsche Labels: *veraltet* / *Duplikat* / *bereits gelöst*).
* Wenn ``triage.auto_close`` und Capability ``close_issue`` erlaubt →
  ``gh issue close`` mit ``--reason completed`` (für
  ``already_solved``) oder ``--reason "not planned"`` (sonst).
* ``execute_run`` wird **nicht** aufgerufen — der board-loop
  appended eine Triage-Skip-Row in die Summary-Tabelle und
  iteriert weiter.

Side-Effect-Fehler (``gh``-Crash) werden geloggt, blocken den
board-loop aber nicht. Triage ist additiv: ein nicht auto-closendes
Issue bleibt offen für die nächste Iteration.

### 6.4 PlanProposed im PR-Body

``execute_run`` lädt nach erfolgreicher PR-Erstellung das jüngste
``PlanProposed``-Event des Runs aus dem Store, zieht den Plan-Markdown
aus dem Blob-Store (``artifacts.plan``) und reicht ihn an
``render_pr_body(plan_md=...)`` durch. Der PR-Body bekommt eine
``### Plan``-Sektion:

* Pläne ≤ 2 KB inline.
* Längere Pläne in ``<details>``-Element eingeklappt.

Bei fehlendem Plan oder fehlendem Blob (CAS-GC) wird die Sektion
weggelassen — kein Crash, keine halben Reste.

---

## Teil 7 — Konfiguration: ``triage:``-Block

Neu in ``ProjectSpec`` (default-konstruiert, also Spec-kompatibel zu
v0.3):

```yaml
triage:
  enabled: false        # Pre-Phase Opt-in. Default: aus.
  model: null           # null = nutze model aus issue_label-Trigger.
  max_turns: 4          # Tool-Turns-Cap für Triage-Aufruf.
  auto_comment: true    # gh issue comment beim Skip
  auto_close: true      # gh issue close beim Skip
```

**Operator-Tipp:** kleines Modell für Triage (``claude-haiku-4-5``)
reicht in der Regel — Klassifikation, kein Code. Großes Modell nur
nutzen, wenn man Triage-Treffsicherheit messen will.

---

## Teil 8 — Was sich NICHT geändert hat

- 5-Phasen-Pipeline pro dispatched Run (Plan → Implement → Validate →
  Eval → Decide) ist unverändert. Triage ist eine **Pre-Phase**, die
  außerhalb der Pipeline läuft.
- ``payload_schema_version`` bestehender Kinds bleibt ``"1.0"``.
- Capabilities ``merge_pr``, ``push_to_main``, ``push_force`` bleiben
  ``Literal[False]``.
- Self-Improvement-Verbot (Prinzip 3) unangetastet — forge ändert
  weiterhin nie ihre eigene Konfiguration.
- Cost-Caps-Struktur unverändert. Triage-Kosten zählen in
  ``per_run_usd`` / ``per_project_per_day_usd`` mit (sie sind ein
  Mini-Run mit eigener ``run_id``).

---

## Anhang — Migration v0.3 → v0.4

Bestehende ``.forge/project.yaml``-Specs sind **unverändert gültig**.
Wer Triage benutzen will, fügt:

```yaml
triage:
  enabled: true
  model: claude-haiku-4-5
```

hinzu. Ohne diesen Block läuft der board-loop genau wie in v0.3
(post-Board-Trigger-Add).

Bestehende Events bleiben lesbar (kein Schema-Break). Replay-Tests
müssen die ``len(EventKind) == 18``-Invariante kennen.
