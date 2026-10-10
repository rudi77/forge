# forge auf einer neuen Maschine installieren

forge selbst ist **ein einzelnes Programm** (Python ist eingebaut). Es steuert
aber externe Werkzeuge, die auf der Maschine vorhanden sein müssen.

## 1. Voraussetzungen

| Werkzeug | Wofür | Prüfen |
|---|---|---|
| **git** | Worktrees, Branches, Commits | `git --version` |
| **Claude Code CLI** (`claude`, braucht Node.js ≥ 18) | der Coding-Agent | `npm install -g @anthropic-ai/claude-code`, dann `claude --version` |
| Anmeldung für Claude | Agent-Aufrufe | `claude login` (Abo) **oder** Umgebungsvariable `ANTHROPIC_API_KEY` |
| **gh** (bei GitHub) | Issues, PRs, Releases | `gh auth login`, dann `gh auth status` |
| **az** + Extension `azure-devops` (bei Azure DevOps) | Work Items, PRs, Pipelines | `az extension add --name azure-devops`; Auth über `az login` oder `AZURE_DEVOPS_EXT_PAT` |
| die Werkzeuge deines Projekts | Eval-Suite (z.B. `pytest`, `npm test`) | wie im Projekt üblich |

## 2. forge installieren

### Windows — Installer (empfohlen)

1. `forge-setup.exe` herunterladen: GitHub → **Releases** (bei jedem Tag `v*`)
   oder → **Actions** → letzter CI-Lauf → Artefakt `forge-windows-x64`.
2. Doppelklick. Der Installer braucht **keine Adminrechte**, installiert nach
   `%LOCALAPPDATA%\Programs\forge` und trägt forge in den Benutzer-PATH ein.
3. **Neues** Terminal öffnen → `forge --help`.

Deinstallieren: *Einstellungen → Apps → forge*. Dabei wird auch der
PATH-Eintrag entfernt.

### Windows — nur die exe

`forge.exe` aus demselben Release/Artefakt in einen Ordner legen, der im PATH
liegt (oder den Ordner zum PATH hinzufügen). Mehr ist nicht nötig.

### Linux / macOS

Variante A, als Binary bauen (eine Datei, kein Python nötig auf der Zielmaschine):

```bash
git clone https://github.com/rudi77/forge && cd forge
uv run --with pyinstaller python packaging/build_binary.py
sudo install dist/forge /usr/local/bin/forge      # oder ~/.local/bin
```

Variante B, als Python-Tool über uv (aktualisierbar):

```bash
git clone https://github.com/rudi77/forge && cd forge
scripts/install.sh            # Windows-Pendant: pwsh scripts/install.ps1
```

> Das Binary wird pro Betriebssystem gebaut. Eine `forge.exe` läuft nur auf
> Windows, ein unter Linux gebautes `forge` nur auf Linux.

## 3. In einem Projekt einrichten

```bash
cd mein-projekt                 # ein git-Repository
forge init                      # legt .forge/project.yaml an (gültige Defaults)
#   → surfaces, eval_suites, cost_caps anpassen
forge doctor                    # prüft Spec, Werkzeuge, Anmeldung
forge run --dry-run --prompt "noop"   # Probelauf ohne Agent und ohne Kosten
```

Erster echter Lauf gegen ein Issue: `docs/USER_GUIDE.md` §5.

Für die Fabrik (Board + Conductor):

```bash
forge doctor --board --fix      # legt die forge:-Labels/Tags im Tracker an
forge board-loop --watch --conductor --interval 300
```

## 4. Aktualisieren

- Installer: neue `forge-setup.exe` einfach drüber installieren.
- exe/Binary: Datei ersetzen.
- uv-Variante: `git pull && scripts/install.sh`.

Die Projektdaten (`.forge/events.duckdb`, Blobs, Logs) liegen im jeweiligen
Repository und bleiben beim Update erhalten.

## 5. Neues Release bauen (für Maintainer)

```bash
git tag v0.7.0 && git push origin v0.7.0
```

Die CI testet, baut `forge.exe` und `forge-setup.exe` (Windows-Runner),
macht einen Smoke-Test (`init` + `doctor` in einem frischen Repo) und hängt
beide Dateien an das GitHub-Release des Tags.
