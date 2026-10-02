# forge — Spezifikation v0.7

> Status: Working Draft v0.7
> Letzte Änderung: 2026-10-02
> Vorgänger: [`forge-spec-v0.6.md`](forge-spec-v0.6.md) — bleibt als
> historisches Dokument im Repo
> Diff-Doku: dieses Dokument beschreibt **nur** die Änderungen gegenüber
> v0.6. Mantras, Pipeline-Phasen der Loop, Kostenebenen, Judge, Memory,
> Resume usw. gelten unverändert, soweit hier nicht explizit etwas
> umformuliert wird. Design + Begründungen: [`sdlc-factory-roadmap.md`](sdlc-factory-roadmap.md).

> **Änderungen gegenüber v0.6** (zusammengefasst): forge bildet den SDLC
> vom Wunsch bis zum Release ab und ist nicht mehr an GitHub gebunden.
> **Anbieter-Schnittstellen** (`WorkTracker`/`CodeHost`) mit GitHub und
> **Azure DevOps**; der Conductor **arbeitet nach** (`request_changes`),
> **repariert roten CI**, **zieht Geschwister-PRs nach**; forge **erzeugt
> Arbeit** (Specs, Epics, Issues, Bugs) hinter opt-in-Leitplanken; ein
> **Arbeitsgraph** mit Konflikt-Scheduling über Worktrees; ein
> **Release-Train** mit SemVer + Changelog. Neuer EventKind:
> `WorkItemCreated` → **`len(EventKind) == 27`**.

---

## Teil 1 — Was sich am Fließband ändert

```
 Wunsch / schedule / CI rot auf main / Funde aus Runs        (Arbeit erzeugen)
            │
            ▼
 forge:proposed ──(Mensch gibt frei)──► forge:epic ──(Zerlegung)──► forge:tracking
                                                    │ Kinder
                                                    ▼
 forge:requirements → forge:design → forge:ready → forge:in-dev ⇄ forge:qa
        (Spec-PR)                     (Touches-     ▲  request_changes │
                                       Scheduling)  │  CI rot          │ merged
                                                    │  Merge-Konflikt  ▼
                                                    └──────────── forge:release
                                                                     │ Train / Tag
                                                                     ▼
                                                                 forge:done
                         forge:blocked von überall (nur ein Mensch hebt es auf)
```

| Übergang | Signal | neu |
|---|---|---|
| `requirements → design` | `RequirementsRefined` **und** Spec-PR gemergt (falls einer existiert) | Spec-Gate |
| `in-dev → qa` | PR offen **und** kein aktuelles `request_changes`, CI nicht rot, kein Konflikt | Gates |
| `qa → in-dev` | aktuelles `request_changes` / CI rot / `CONFLICTING` | ✅ |
| `qa → blocked` | > `MAX_REWORK_ROUNDS` (2) Runden / ≥ `MAX_CI_FIX_ATTEMPTS` (2) | ✅ |
| `epic → tracking` | `WorkItemCreated(parent=Epic, source=epic_decomposition)` | ✅ |
| `tracking → done` | alle Kinder `done` | ✅ |
| `tracking → qa` | Integrations-PR `forge/epic-<N> → main` offen | ✅ |
| `release → done` | `ReleaseTagged` mit `issue_number` **oder** in `issue_numbers` | Train |

`forge:proposed` verlässt der Conductor **nie** selbst (menschliches Gate).

## Teil 2 — Anbieter (GitHub, Azure DevOps, …)

```yaml
provider:
  tracker: azure_devops        # github (Default) | azure_devops
  code_host: github            # optional, Default = tracker (gemischt erlaubt)
  azure:
    organization: contoso      # oder https://dev.azure.com/contoso
    project: Shop
    repository: shop-web       # Default: Projektname
    closed_state: Closed       # Scrum: Done
    work_item_types: {story: "Product Backlog Item"}
```

- `board.owner`/`board.project_number` sind nur bei GitHub Pflicht;
  `board.provider` erbt `provider.tracker`.
- Azure: Stages = Work-Item-**Tags**, Abhängigkeiten = `Depends-On:` im
  Body, Review = Vote + Kommentar-Thread, CI = Build-/Status-Policies,
  Release = annotierter Git-Tag. Auth `AZURE_DEVOPS_EXT_PAT` / `az login`.
- `forge doctor --board [--fix]` prüft/legt alle `forge:`-Labels an.
- Azure-Pipelines-Vorlagen: `forge_adapters/azure/templates/`.

## Teil 3 — Capabilities & Spec-Blöcke (additiv)

| Feld | Default | Zweck |
|---|---|---|
| `capabilities.create_work_items` | `false` | forge darf Work-Items anlegen |
| `intake.max_items_per_epic` | `8` | Obergrenze pro Zerlegung/Fund-Block |
| `intake.max_items_per_day` | `20` | Obergrenze aller Quellen in 24 h |
| `intake.auto_accept` | `[]` | Typen, die ohne Freigabe starten |
| `intake.watch_main_ci` | `null` | Branch, dessen roter CI ein Bug-Item erzeugt |
| `intake.spec_dir` / `spec_publish` | `docs/specs` / `auto` | Spec-PR vs. Kommentar |
| `release.mode` | `per_issue` | `train` = Release-Train |
| `release.schedule` / `min_items` | `null` / `1` | wann der Train fährt |
| `release.version_files` / `tag_prefix` | `[]` / `v` | Versions-Bump |

`push_to_main`/`push_force` bleiben `Literal[False]`. Neu erlaubt (über die
bestehenden Capabilities `open_pr`/`merge_pr`/`create_release`): **Pushes auf
bestehende `forge/*`-Branches**, nur fast-forward (`git_push_argv` erzwingt
beides; menschliche Branches und `main` nie).

## Teil 4 — Events (Schema-Bilanz)

| Kind | Version | Änderung |
|---|---|---|
| `WorkItemCreated` | **1.0 (neu)** | number, kind, title, source, stage, parent, depends_on, expected_files, fingerprint, origin_issue, provider |
| `RunStarted` | 1.0 → **1.1** | `trigger="rework"`, `provider` |
| `PRReviewed` | 1.0 → **1.1** | `reasoning_blob` (Kontext für die Nacharbeit) |
| `WorkItemBlocked` | 1.1 → **1.2** | kinds `rework_exhausted`, `rework_no_change`, `ci_fix_exhausted`, `file_conflict`, `merge_conflict` |
| `ReleaseTagged` | 1.0 → **1.1** | `version`, `issue_numbers`, `integrated_into` |
| `ConductorTickCompleted` | 1.1 → **1.2** | `parallel_running`, `capacity` |

Alle additiv; alte Events lesen weiter. `RequirementsRefined` bleibt 1.0 —
die Spec liegt schon als `artifacts["spec"]`, der Spec-PR ist ein normales
`PRCreated` mit Label `forge:spec`.

Neue Views: `factory_intake`, `factory_parallelism`, `factory_releases`
(+ Sektionen in `forge analyze`).

## Teil 5 — Nacharbeit, CI-Fix, Konflikte (ein Primitiv)

Alle drei laufen als **Run auf dem bestehenden PR-Branch**: der Runner bekommt
den frisch geholten PR-Head als `base_ref` (unverändert, Mantra 3), das
Ergebnis wird fast-forward auf den PR-Head gepusht. Neuer Head → altes
Review/CI veraltet → `in-dev → qa`. Höchstens ein Run pro Review bzw. Head;
scheitert er (oder der Push), wird nach `blocked` eskaliert.

Merge-Konflikte (`CONFLICTING` nach einem Geschwister-Merge): erst
deterministischer `git merge` der Basis (sauber → Push, kein LLM); bei
Konflikten startet ein Agent-Run ab dem lokal committeten Konflikt-Stand —
die Auflösung ist ein gewöhnlicher rot→grün-KEEP.

Beim Code-Host beobachtete, aber nicht als Event erfasste Merges (Mensch,
Azure ohne Webhook) trägt der Conductor als `PRMerged(merger="external")` nach.

## Teil 6 — Arbeit erzeugen

- **Specs:** die verdichteten Akzeptanzkriterien werden Spec-PR
  (`docs/specs/<n>-<slug>.md`; Features/Epics/Stories) oder Kommentar
  (Bugs/Tasks). Gemergte Spec-Datei gewinnt über den ursprünglichen Text; alle
  folgenden Runs bekommen sie als Akzeptanzkriterium.
- **`FORGE-WORKITEMS`-Block** (YAML) im Orchestrator- und Review-Prompt; der
  Runner reicht ihn nur durch, angelegt wird in Loop 2 (`intake.py`).
- **Quellen:** Epic-Zerlegung, Run-Funde, Review-Folgeaufgaben, CI rot auf
  `intake.watch_main_ci`, fällige `triggers.schedule` (erzeugen ein Epic).
- **Leitplanken:** opt-in Capability, Obergrenzen, Fingerprint-Dedupe
  (`forge-fingerprint: sha256:…` im Body), Start in `forge:proposed`, Label
  `forge:generated`.

## Teil 7 — Arbeitsgraph über Worktrees

- `Touches: src/a/**, …` im Body (von der Zerlegung geschrieben, editierbar).
- Neue Code-Runs mit überlappenden `Touches` laufen nicht parallel zueinander
  oder zu offenen PRs mit überlappenden `Touches` (`file_conflict`); ohne
  `Touches` läuft ein Code-Run nie gleichzeitig mit einem anderen.
- Kapazität = `min(--max-parallel, Tagesbudget-Rest / ⌀Run-Kosten)`, bei
  < 2 GB freiem Platz höchstens 1.
- `Integration: branch` im Epic-Body → Kinder mergen nach `forge/epic-<N>`,
  ein Sammel-PR nach `main` geht durch die normale QA.

## Teil 8 — Release-Train

`release.mode: train`: ab `min_items` (optional nach `schedule`) öffnet forge
einen Release-PR `chore(release): vX.Y.Z` (SemVer aus Conventional Commits,
Changelog-Abschnitt, `version_files`). Er läuft durch Agent-Review +
`merge_pr`-Gates oder einen Menschen; nach dem Merge Tag + Release auf der
Basis und `ReleaseTagged(issue_numbers=[…])` für alle enthaltenen Items.

## Teil 9 — Was sich NICHT geändert hat

- Runner, Scoring, Gates, Keep/Discard: unverändert (Mantra 3). Alles Neue
  lebt in Loop 2 (`forge-cli`), in den Adaptern und in additiven Spec-Feldern.
- Kein Push auf `main`, kein Force-Push, keine Self-Improvement.
- PBS/Bandit weiter gegated auf ≥100/≥300 Runs.
