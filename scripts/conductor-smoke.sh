#!/usr/bin/env bash
# Live-Durchstich des Conductors gegen ein echtes Sandbox-Repo (Roadmap L0).
#
# Was passiert:
#   1. `forge doctor --board --fix` legt alle forge:<stage>-Labels/Tags an.
#   2. Ein triviales Issue/Work-Item startet in forge:design.
#   3. `forge board-loop --watch --conductor` läuft N Ticks und schiebt es durchs
#      Fließband (design → ready → in-dev → qa → release → done).
#   4. Am Ende werden Stage + Event-Zähler ausgegeben.
#
# Voraussetzungen im Sandbox-Repo (NIE gegen ein Produktiv-Repo laufen lassen):
#   - .forge/project.yaml mit board:-Block und — für den vollen Durchlauf —
#     capabilities.merge_pr: true und capabilities.create_release: true
#   - gh (GitHub) bzw. az + AZURE_DEVOPS_EXT_PAT (Azure DevOps) eingeloggt
#   - ANTHROPIC_API_KEY gesetzt
#
# Verwendung:
#   scripts/conductor-smoke.sh <sandbox-repo-pfad> [ticks] [interval_s]
set -euo pipefail

REPO="${1:?Pfad zum Sandbox-Repo angeben}"
TICKS="${2:-12}"
INTERVAL="${3:-60}"

cd "$REPO"
echo "== 1/4 doctor --board --fix"
forge doctor --board --fix

echo "== 2/4 Smoke-Issue anlegen (forge:design)"
PROVIDER="$(python3 - <<'PY'
import yaml
spec = yaml.safe_load(open(".forge/project.yaml", encoding="utf-8"))
print((spec.get("provider") or {}).get("tracker", "github"))
PY
)"
TITLE="forge smoke $(date -u +%Y%m%dT%H%M%SZ)"
BODY="Lege die Datei SMOKE.md mit dem Inhalt 'ok' an.

Akzeptanzkriterium: SMOKE.md existiert und enthält 'ok'."
if [ "$PROVIDER" = "azure_devops" ]; then
    az boards work-item create --type Task --title "$TITLE" \
        --description "$BODY" --fields "System.Tags=forge:design" -o none
else
    gh issue create --title "$TITLE" --body "$BODY" --label forge:design
fi

echo "== 3/4 Conductor ($TICKS Ticks, ${INTERVAL}s)"
forge board-loop --watch --conductor --interval "$INTERVAL" --max-ticks "$TICKS"

echo "== 4/4 Ergebnis"
forge analyze || true
echo "Prüfe im Tracker, ob '$TITLE' auf forge:done steht."
