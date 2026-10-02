"""Baut ``WorkTracker``/``CodeHost`` aus der Spec (``provider:``-Block).

Der einzige Ort, an dem ein Anbieter-Name auf eine Implementierung abgebildet
wird. ``forge-cli`` ruft nur :func:`build_tracker`/:func:`build_code_host` und
kennt danach keinen Anbieter mehr.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from forge_core.spec import ProjectSpec

from forge_adapters.base import CodeHost, TrackerError, WorkTracker

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


def build_tracker(
    spec: ProjectSpec,
    repo_root: Path,
    *,
    run_subprocess: SubprocessRunner = subprocess.run,
) -> WorkTracker:
    kind = spec.provider.tracker
    if kind == "github":
        from forge_adapters.github.repo import detect_github_slug
        from forge_adapters.github.tracker import GitHubTracker

        owner, name = detect_github_slug(repo_root, run_subprocess=run_subprocess)
        return GitHubTracker(
            owner=owner, repo=name, repo_root=repo_root, run_subprocess=run_subprocess
        )
    if kind == "azure_devops":
        from forge_adapters.azure import AzureBoardsTracker

        assert spec.provider.azure is not None  # Spec-Validierung garantiert das
        return AzureBoardsTracker(config=spec.provider.azure, run_subprocess=run_subprocess)
    raise TrackerError(f"unknown tracker provider {kind!r}")


def build_code_host(
    spec: ProjectSpec,
    repo_root: Path,
    *,
    run_subprocess: SubprocessRunner = subprocess.run,
) -> CodeHost:
    kind = spec.provider.effective_code_host
    if kind == "github":
        from forge_adapters.github.code_host import GitHubCodeHost

        return GitHubCodeHost(repo_root=repo_root, run_subprocess=run_subprocess)
    if kind == "azure_devops":
        from forge_adapters.azure import AzureReposCodeHost

        assert spec.provider.azure is not None
        return AzureReposCodeHost(
            config=spec.provider.azure, repo_root=repo_root, run_subprocess=run_subprocess
        )
    raise TrackerError(f"unknown code-host provider {kind!r}")
