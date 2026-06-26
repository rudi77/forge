#!/usr/bin/env bash
# Installiert die `forge`-CLI global, sodass sie in jedem Repo aufrufbar ist.
#
# Baut die vier Workspace-Wheels (forge-core/-execute/-adapters/-cli) und
# installiert sie via `uv tool install` PER DATEIPFAD. Der Pfad-Install ist
# Absicht: die Distributionsnamen `forge-cli`, `forge-core` und `forge-adapters`
# sind auf PyPI von FREMDEN Paketen belegt — ein Install per Name würde die
# falschen Pakete ziehen. Direkte Wheel-Pfade sind in uv gepinnte Referenzen und
# überschreiben jede Index-Version.
#
# Ergebnis: ein `forge`-Shim auf der PATH (von uv verwaltet). Erneutes Ausführen
# aktualisiert die Installation (idempotent via --reinstall).
#
# Verwendung:
#   scripts/install.sh              # baut + installiert
#   scripts/install.sh --uninstall  # entfernt forge wieder
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

require_uv() {
    if ! command -v uv >/dev/null 2>&1; then
        echo "uv ist nicht installiert oder nicht auf der PATH." >&2
        echo "Siehe https://docs.astral.sh/uv/getting-started/installation/" >&2
        exit 1
    fi
}

if [[ "${1:-}" == "--uninstall" ]]; then
    require_uv
    echo "Entferne forge-cli ..."
    uv tool uninstall forge-cli
    echo "forge entfernt."
    exit 0
fi

require_uv

# Wheels in ein isoliertes Unterverzeichnis bauen, getrennt vom
# PyInstaller-Output (dist/forge). dist/ ist gitignored.
WHEEL_DIR="$ROOT/dist/wheels"
rm -rf "$WHEEL_DIR"
mkdir -p "$WHEEL_DIR"

echo "[1/3] Baue Workspace-Wheels -> $WHEEL_DIR"
uv build --all-packages --out-dir "$WHEEL_DIR"

get_wheel() {
    local prefix="$1" hit
    hit="$(ls "$WHEEL_DIR/${prefix}-"*.whl 2>/dev/null | head -n1 || true)"
    if [[ -z "$hit" ]]; then
        echo "Wheel für '$prefix' nicht gefunden in $WHEEL_DIR" >&2
        exit 1
    fi
    printf '%s' "$hit"
}

CLI="$(get_wheel forge_cli)"
CORE="$(get_wheel forge_core)"
EXECUTE="$(get_wheel forge_execute)"
ADAPTERS="$(get_wheel forge_adapters)"

echo "[2/3] Installiere forge als uv-Tool (Wheels per Pfad, PyPI-Kollision umgangen)"
uv tool install --reinstall "$CLI" --with "$CORE" --with "$EXECUTE" --with "$ADAPTERS"

echo "[3/3] Stelle sicher, dass das uv-Tool-bin auf der PATH liegt"
uv tool update-shell

echo
echo "forge installiert. Test aus einem beliebigen Verzeichnis:"
echo "    forge --help"
echo
echo "Falls 'forge' noch nicht gefunden wird: neue Shell öffnen (PATH-Refresh)."
echo "Deinstallieren:  scripts/install.sh --uninstall"
