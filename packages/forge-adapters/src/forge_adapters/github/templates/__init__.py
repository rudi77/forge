"""GitHub Action Workflow-Templates.

Liegen als YAML neben dieser Datei. Werden via `templates_path()` ausgeliefert,
damit `forge init` sie in `.github/workflows/` des Zielprojekts kopieren kann.
"""

from importlib.resources import files
from pathlib import Path


def templates_path() -> Path:
    """Liefert den Pfad zum Templates-Verzeichnis innerhalb des Packages."""
    return Path(str(files("forge_adapters.github.templates")))


def list_templates() -> list[Path]:
    root = templates_path()
    return sorted(p for p in root.glob("*.yml"))
