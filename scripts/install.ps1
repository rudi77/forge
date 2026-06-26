<#
.SYNOPSIS
    Installiert die `forge`-CLI global, sodass sie in jedem Repo aufrufbar ist.

.DESCRIPTION
    Baut die vier Workspace-Wheels (forge-core/-execute/-adapters/-cli) und
    installiert sie via `uv tool install` PER DATEIPFAD. Der Pfad-Install ist
    Absicht: die Distributionsnamen `forge-cli`, `forge-core` und
    `forge-adapters` sind auf PyPI von FREMDEN Paketen belegt — ein Install per
    Name würde die falschen Pakete ziehen. Direkte Wheel-Pfade sind in uv
    gepinnte Referenzen und überschreiben jede Index-Version.

    Ergebnis: ein `forge`-Shim auf der PATH (von uv verwaltet). Erneutes
    Ausführen aktualisiert die Installation (idempotent via --reinstall).

.PARAMETER Uninstall
    Entfernt die installierte forge-CLI wieder (`uv tool uninstall forge-cli`).

.EXAMPLE
    pwsh scripts/install.ps1
    # baut + installiert; danach `forge --help` aus jedem Verzeichnis

.EXAMPLE
    pwsh scripts/install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

# Repo-Root = Elternverzeichnis dieses Skripts (scripts/ liegt direkt unter Root).
$Root = Split-Path -Parent $PSScriptRoot

function Assert-Uv {
    if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
        throw "uv ist nicht installiert oder nicht auf der PATH. Siehe https://docs.astral.sh/uv/getting-started/installation/"
    }
}

if ($Uninstall) {
    Assert-Uv
    Write-Host "Entferne forge-cli ..." -ForegroundColor Cyan
    uv tool uninstall forge-cli
    Write-Host "forge entfernt." -ForegroundColor Green
    return
}

Assert-Uv

# Wheels in ein isoliertes Unterverzeichnis bauen, damit sie nicht mit dem
# PyInstaller-Output (dist/forge.exe) kollidieren. dist/ ist gitignored.
$WheelDir = Join-Path $Root 'dist/wheels'
if (Test-Path $WheelDir) { Remove-Item -Recurse -Force $WheelDir }
New-Item -ItemType Directory -Force -Path $WheelDir | Out-Null

Write-Host "[1/3] Baue Workspace-Wheels -> $WheelDir" -ForegroundColor Cyan
uv build --all-packages --out-dir $WheelDir | Out-Host

function Get-Wheel([string]$Prefix) {
    $w = Get-ChildItem -Path $WheelDir -Filter "$Prefix-*.whl" | Select-Object -First 1
    if (-not $w) { throw "Wheel für '$Prefix' nicht gefunden in $WheelDir" }
    return $w.FullName
}

$cli      = Get-Wheel 'forge_cli'
$core     = Get-Wheel 'forge_core'
$execute  = Get-Wheel 'forge_execute'
$adapters = Get-Wheel 'forge_adapters'

Write-Host "[2/3] Installiere forge als uv-Tool (Wheels per Pfad, PyPI-Kollision umgangen)" -ForegroundColor Cyan
uv tool install --reinstall $cli --with $core --with $execute --with $adapters | Out-Host

Write-Host "[3/3] Stelle sicher, dass das uv-Tool-bin auf der PATH liegt" -ForegroundColor Cyan
uv tool update-shell | Out-Host

Write-Host ""
Write-Host "forge installiert. Test aus einem beliebigen Verzeichnis:" -ForegroundColor Green
Write-Host "    forge --help"
Write-Host ""
Write-Host "Falls 'forge' noch nicht gefunden wird: neue Shell öffnen (PATH-Refresh)." -ForegroundColor Yellow
Write-Host "Deinstallieren:  pwsh scripts/install.ps1 -Uninstall"
