"""PyInstaller-Einstiegspunkt für das gebündelte `forge`-Binary.

PyInstaller braucht ein konkretes Skript als Startpunkt — der
`project.scripts`-Eintrag (`forge = "forge_cli.main:main"`) ist für das
Wheel, nicht für den Bundler. Dieses Modul ist bewusst minimal: es ruft
nur `forge_cli.main.main` auf, damit die gesamte CLI-Logik im Package
bleibt und nicht ins Packaging abwandert (Schichtung, siehe CLAUDE.md).
"""

from __future__ import annotations

from forge_cli.main import main

if __name__ == "__main__":
    main()
