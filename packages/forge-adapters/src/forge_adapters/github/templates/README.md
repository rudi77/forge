# forge GitHub Action Templates

YAML-Workflows, einer pro Trigger-Typ aus Spec Teil 8.1:

| Datei | Trigger | Was es tut |
|---|---|---|
| `forge-issue-trigger.yml` | Issue-Label `auto-fix` / `auto-feature` | Run mit `--trigger issue_label`, PR via `--create-pr` |
| `forge-ci-autofix.yml` | CI-Run schlägt fehl | Reaktiv: `--trigger ci_failure`, max 3 Iterations |
| `forge-nightly.yml` | Cron `0 2 * * *` oder manuell | Schedule-Run, default Draft-PR |
| `forge-pr-review.yml` | forge:auto-PR geöffnet/aktualisiert oder Checks fertig | Reaktiv: `forge review-pr` (Agent-Review + opt-in Merge) |
| `forge-pr-merged.yml` | PR mit Label `forge:auto` wird gemerged | Schreibt `PRMerged`-Event in den Store |

## Setup

1. Diese Files nach `.github/workflows/` deines Projekts kopieren
2. Secrets setzen:
   - `ANTHROPIC_API_KEY` — für Claude
   - `GITHUB_TOKEN` ist automatisch verfügbar, braucht aber `pull-requests: write` (Settings → Actions → General → Workflow permissions)
3. `.forge/project.yaml` committen (Beispiel: `examples/pinta/.forge/project.yaml`)
4. Erstes Issue mit Label `auto-fix` öffnen — am besten eines, von dem du
   weißt, dass es lösbar ist, aber nicht trivial fällt.

## Agent-Review-Merge (opt-in)

`forge-pr-review.yml` lässt einen Agent offene `forge:auto`-PRs reviewen und
merged sie **opt-in selbst** — nur wenn `capabilities.merge_pr: true` in der
project.yaml steht UND der Agent `approve` gibt UND der CI grün ist. Default
(`merge_pr: false`) → der Workflow reviewt nur, merged nie. `push_to_main` /
`push_force` bleiben kategorisch deaktiviert.

## Was NICHT in v1

- Push auf `main`, Force-Push (capabilities-hardcoded false)
- Slack-Notifications (kommt in M2)
- Persistenz des Event-Stores zwischen Runs auf GitHub Actions
  (lokale `.forge/events.duckdb` reicht für v1; zentral in v3)

## Sicherheit

Issue-Bodies sind UNTRUSTED — sie werden in den Templates explizit als
`UNTRUSTED USER CONTENT` markiert (Spec Teil 7.3). Capabilities aus der
project.yaml gelten zusätzlich zu den GitHub-Permissions.
