"""Shared CLI utilities — Projekt-Discovery, Versions-Stamp, Console.

Bewusst klein gehalten. Jede Subcommand-Datei holt sich von hier ihre
Common-Inputs (Spec, EventStore, BlobStore, factory_version, project_fingerprint).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge_core.blobs import BlobStore
from forge_core.spec import ProjectSpec, load_spec
from forge_core.store import EventStore
from rich.console import Console

console = Console()
err_console = Console(stderr=True)


@dataclass
class ForgeContext:
    """Aufgelöste Pfade + Spec für einen CLI-Aufruf."""

    repo_root: Path
    forge_dir: Path
    spec: ProjectSpec
    spec_path: Path
    factory_version: str
    project_fingerprint: str
    store_path: Path
    blobs_path: Path

    def open_store(self) -> EventStore:
        return EventStore(self.store_path)

    def open_blobs(self) -> BlobStore:
        return BlobStore(self.blobs_path)

    @property
    def memory_seed_path(self) -> Path:
        """Optional operator-authored project memory seed (``.forge/memory.md``)."""
        return self.forge_dir / "memory.md"


class ContextError(RuntimeError):
    """Pfad-/Spec-Auflösung fehlgeschlagen."""


def find_repo_root(start: Path | None = None) -> Path:
    """Sucht nach einem `.git`-Verzeichnis ab `start` aufwärts.

    Default: aktuelles Arbeitsverzeichnis. Wirft `ContextError`, wenn keins
    gefunden wird — `forge` braucht ein Git-Repo, weil Worktrees darauf
    basieren.
    """
    cur = (start or Path.cwd()).resolve()
    for candidate in [cur, *cur.parents]:
        if (candidate / ".git").exists():
            return candidate
    raise ContextError(
        f"no git repository found from {cur} upwards — forge requires one"
    )


def load_context(
    *,
    spec_path: Path | None = None,
    repo_root: Path | None = None,
) -> ForgeContext:
    """Lädt die Spec und baut den `ForgeContext`.

    - `spec_path` default: `<repo_root>/.forge/project.yaml`
    - `repo_root` default: aufwärts-Suche ab CWD
    """
    repo = (repo_root or find_repo_root()).resolve()
    forge_dir = repo / ".forge"
    actual_spec = (spec_path or forge_dir / "project.yaml").resolve()
    if not actual_spec.is_file():
        raise ContextError(
            f"spec not found at {actual_spec}. "
            f"Run `forge init` to create one or pass --spec."
        )

    spec = load_spec(actual_spec)
    return ForgeContext(
        repo_root=repo,
        forge_dir=forge_dir,
        spec=spec,
        spec_path=actual_spec,
        factory_version=detect_factory_version(repo),
        project_fingerprint=compute_project_fingerprint(spec, repo),
        store_path=forge_dir / "events.duckdb",
        blobs_path=forge_dir / "blobs",
    )


def detect_factory_version(repo: Path) -> str:
    """Liefert die forge-Version, mit der dieser Run läuft.

    In v1: Git-SHA des forge-Repos, falls forge aus dem Repo ausgeführt wird;
    sonst Package-Version aus `forge_core.__version__`.
    """
    # Wenn wir IN einem forge-Checkout laufen (CWD ist forge selbst),
    # nehmen wir HEAD-SHA. In v1 ist das das normale Setup.
    forge_repo = _find_forge_repo()
    if forge_repo is not None:
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(forge_repo),
                capture_output=True,
                text=True,
                check=True,
                encoding="utf-8",
                errors="replace",
            )
            return f"git:{result.stdout.strip()[:12]}"
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass
    from forge_core import __version__

    return f"pkg:{__version__}"


def _find_forge_repo() -> Path | None:
    """Findet den forge-Quellbaum, wenn forge editierbar installiert ist.

    Heuristik: ist der `forge_core`-Modulpfad innerhalb eines Git-Repos namens
    `forge`?
    """
    try:
        import forge_core
    except ImportError:
        return None
    src = Path(forge_core.__file__).resolve()
    for ancestor in [src.parent, *src.parents]:
        if ancestor.name in ("forge", "forge-core"):
            if (ancestor.parent / ".git").exists():
                return ancestor.parent
            if (ancestor / ".git").exists():
                return ancestor
    return None


def compute_project_fingerprint(spec: ProjectSpec, repo: Path) -> str:
    """Hash über `{lang, framework, file_count}` als project_fingerprint.

    Spec Teil 4.1 — wird später (v3) für Bandit-Konditionierung gebraucht.
    Stabilität: derselbe Repo-Snapshot ergibt denselben Hash unabhängig vom
    Working Tree.
    """
    lang = sorted(spec.language_stack)
    fw = sorted(spec.frameworks)
    file_count = _count_files(repo)
    payload = f"{lang}|{fw}|{file_count}".encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _count_files(repo: Path, *, max_depth: int = 6) -> int:
    """Grobe Datei-Anzahl, ignoriert .git, .venv, node_modules, .forge."""
    count = 0
    skip_dirs = {".git", ".venv", "venv", "node_modules", ".forge", "__pycache__"}
    for root, dirs, files in os.walk(str(repo)):
        depth = Path(root).relative_to(repo).parts
        if len(depth) > max_depth:
            dirs.clear()
            continue
        dirs[:] = [d for d in dirs if d not in skip_dirs]
        count += len(files)
    return count
