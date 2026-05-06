"""venv-Auto-Detection für Subprozesse.

Wenn forge ein Eval-/Preflight-/Coding-Agent-Kommando in einem Worktree ausführt,
nutzt der Subprozess den `python`-Pfad aus dem System-`PATH`. Liegt PINTAs (oder
ein anderes Projekt-) venv unter `<repo-root>/.venv` oder `<repo-root>/venv`,
fehlen dem System-Python alle Projekt-Dependencies — pytest collected 0 Tests,
Eval scheint kaputt zu sein.

Lösung: vor dem Subprozess prependen wir das venv-`Scripts/` (Windows) bzw.
`bin/` (POSIX) an `PATH`. Damit greift ein `python` / `pytest` / `black` aus
dem venv. Cross-platform.

Wichtig: forge greift nur dann ein, wenn die venv tatsächlich existiert.
Sonst bleibt das `env` unangetastet — kein Magic für Projekte ohne venv.
"""

from __future__ import annotations

import os
import platform
from pathlib import Path

_IS_WINDOWS = platform.system() == "Windows"


def find_venv_for(cwd: Path) -> Path | None:
    """Sucht eine venv für `cwd`, indem aufwärts nach `.venv/` oder `venv/`
    gesucht wird.

    Ein Worktree unter `<repo>/.forge/worktrees/<id>/` hat seine venv im
    `<repo>/`-Verzeichnis (forges Worktrees haben keine eigene venv). Der
    Aufwärts-Suche-Pfad findet die richtige.
    """
    cur = Path(cwd).resolve()
    for candidate in [cur, *cur.parents]:
        for name in (".venv", "venv"):
            v = candidate / name
            if (v / ("Scripts" if _IS_WINDOWS else "bin") / _python_name()).is_file():
                return v
    return None


def venv_aware_env(
    cwd: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Liefert ein env-Dict, das die venv (falls vorhanden) im PATH hat.

    Idempotent: wenn die venv schon vorne im PATH steht, bleibt das env
    unverändert. Wenn keine venv gefunden wird, ebenfalls unverändert
    (verhält sich wie `dict(os.environ)`-Stand).
    """
    env = dict(base_env if base_env is not None else os.environ)
    venv = find_venv_for(Path(cwd))
    if venv is None:
        return env

    bin_dir = venv / ("Scripts" if _IS_WINDOWS else "bin")
    bin_str = str(bin_dir)
    sep = os.pathsep
    current = env.get("PATH", "")
    # Idempotenz: schon vorne dran → fertig
    if current.startswith(bin_str + sep) or current == bin_str:
        return env
    env["PATH"] = bin_str + (sep + current if current else "")
    # `VIRTUAL_ENV` setzen, damit Tools wie pip/uv die venv erkennen
    env.setdefault("VIRTUAL_ENV", str(venv))
    return env


def _python_name() -> str:
    return "python.exe" if _IS_WINDOWS else "python"
