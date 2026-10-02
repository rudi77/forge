"""GitHub-Repo-Erkennung aus dem Git-Remote."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from forge_adapters.base import TrackerError
from forge_adapters.github.board import SubprocessRunner

_REMOTE_RE = re.compile(
    r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+?)(?:\.git)?/?$"
)


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """``git@github.com:o/r.git`` / ``https://github.com/o/r`` → (o, r)."""
    match = _REMOTE_RE.search(url.strip())
    if not match:
        return None
    return match.group("owner"), match.group("repo")


def detect_github_slug(
    repo: Path,
    *,
    run_subprocess: SubprocessRunner = subprocess.run,
) -> tuple[str, str]:
    """``git remote get-url origin`` → (owner, repo). ssh und https."""
    result = run_subprocess(
        ["git", "remote", "get-url", "origin"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise TrackerError(f"git remote get-url origin failed: {result.stderr.strip()}")
    url = result.stdout.strip()
    parsed = parse_github_remote(url)
    if parsed is None:
        raise TrackerError(f"could not parse owner/repo from remote URL {url!r}")
    return parsed
