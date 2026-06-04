"""Reproduzierbarer PyInstaller-Build des `forge`-Binaries.

Wird sowohl von der CI (`.github/workflows/ci.yml`) als auch lokal
genutzt — eine einzige Quelle für die Bundle-Flags, damit CI-Artefakt
und lokaler Build identisch sind.

Lokal:

    uv run --with pyinstaller python packaging/build_binary.py

Ergebnis liegt unter `dist/forge` (Linux/macOS) bzw. `dist/forge.exe`
(Windows). PyInstaller leitet die Endung plattformabhängig ab — auf
Windows-Runnern entsteht also `forge.exe`.

Warum die expliziten Flags:

- `--collect-submodules` für alle vier forge-Packages: typer/rich laden
  Subcommands teils lazy, der Bundler sieht sie sonst nicht.
- `--copy-metadata duckdb`: duckdb liest seine Version über
  `importlib.metadata`; ohne die Metadaten crasht der Import im Bundle.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
ENTRY = ROOT / "packaging" / "forge_entry.py"


def build(*, onefile: bool = True, clean: bool = True) -> None:
    args: list[str] = [
        str(ENTRY),
        "--name",
        "forge",
        "--noconfirm",
        "--console",
        "--collect-submodules",
        "forge_core",
        "--collect-submodules",
        "forge_execute",
        "--collect-submodules",
        "forge_adapters",
        "--collect-submodules",
        "forge_cli",
        "--copy-metadata",
        "duckdb",
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build"),
        "--specpath",
        str(ROOT / "build"),
    ]
    if onefile:
        args.append("--onefile")
    if clean:
        args.append("--clean")

    PyInstaller.__main__.run(args)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the forge binary via PyInstaller.")
    parser.add_argument(
        "--onedir",
        action="store_true",
        help="Build a one-folder bundle instead of a single file.",
    )
    parser.add_argument(
        "--no-clean",
        action="store_true",
        help="Keep PyInstaller cache between builds.",
    )
    ns = parser.parse_args()
    build(onefile=not ns.onedir, clean=not ns.no_clean)


if __name__ == "__main__":
    main()
