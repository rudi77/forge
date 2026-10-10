"""Azure-Pipelines-Templates (YAML neben dieser Datei)."""

from importlib.resources import files
from pathlib import Path


def templates_path() -> Path:
    return Path(str(files("forge_adapters.azure.templates")))


def list_templates() -> list[Path]:
    return sorted(templates_path().glob("*.yml"))
