"""PR-Erzeugung via `gh` CLI.

Pro Run mit `decision == pr_created` ruft der Caller `create_pr_for_run()`
auf. Die Funktion:

1. Pusht den Run-Branch zum Remote (`git push -u origin <branch>`)
2. Generiert einen strukturierten PR-Body (Score-Trend, betroffene Files,
   Run-Summary)
3. Ruft `gh pr create` mit Title, Body, Labels
4. Schreibt einen `PRCreated`-Event in den Store
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    build_event,
)
from forge_core.store import EventStore


class GitHubError(RuntimeError):
    """gh CLI oder git-push-Fehler."""


@dataclass(frozen=True)
class PRCreationResult:
    pr_number: int
    url: str
    branch: str


def push_branch(*, repo: Path, branch: str, remote: str = "origin") -> None:
    """`git push -u <remote> <branch>` mit anständigem Error-Reporting."""
    result = subprocess.run(
        ["git", "push", "-u", remote, branch],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"git push -u {remote} {branch} failed: {result.stderr.strip()}"
        )


def create_pr_for_run(
    *,
    repo: Path,
    branch: str,
    title: str,
    body: str,
    base: str = "main",
    labels: list[str] | None = None,
    draft: bool = False,
    push: bool = True,
    gh_bin: str = "gh",
    store: EventStore | None = None,
    run_id: str | None = None,
    project: str = "",
    project_fingerprint: str = "",
    factory_version: str = "",
    spec_version: str = "",
) -> PRCreationResult:
    """Erzeugt einen PR via `gh pr create` und emittiert `PRCreated`.

    Args:
        repo: Pfad zum Repository (das eigentliche, nicht der Worktree).
        branch: Quell-Branch (`forge/<run_id>`).
        title: PR-Titel.
        body: PR-Body als Markdown (siehe `render_pr_body`).
        base: Ziel-Branch (default `main`).
        labels: Liste von Labels (default `["forge:auto"]`).
        push: Wenn True, vorher `git push -u origin <branch>`.
        store: Optional — wenn übergeben, wird `PRCreated` ins Store geschrieben.
            Dann sind `run_id`, `project`, `project_fingerprint`, `factory_version`,
            `spec_version` Pflicht.
    """
    if push:
        push_branch(repo=repo, branch=branch)

    final_labels = labels or ["forge:auto"]

    cmd = [
        gh_bin, "pr", "create",
        "--base", base,
        "--head", branch,
        "--title", title,
        "--body", body,
    ]
    if draft:
        cmd.append("--draft")
    for lbl in final_labels:
        cmd.extend(["--label", lbl])

    result = subprocess.run(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"gh pr create failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    url = result.stdout.strip().splitlines()[-1]
    pr_number = _extract_pr_number(url)

    if store is not None:
        if not (run_id and project and project_fingerprint and factory_version and spec_version):
            raise ValueError(
                "store requires run_id, project, project_fingerprint, "
                "factory_version, spec_version"
            )
        evt = build_event(
            kind=EventKind.PR_CREATED,
            run_id=run_id,
            project=project,
            project_fingerprint=project_fingerprint,
            factory_version=factory_version,
            spec_version=spec_version,
            payload=PRCreatedPayload(
                pr_number=pr_number,
                branch=branch,
                base_branch=base,
                labels=final_labels,
                url=url,
            ),
        )
        store.append(evt)

    return PRCreationResult(pr_number=pr_number, url=url, branch=branch)


# --- PR-Body-Rendering -------------------------------------------------


def render_pr_body(
    *,
    run_id: str,
    focus: str | None,
    decision: str,
    final_score: float | None,
    score_delta: float | None,
    total_cost_usd,
    files_changed: list[str],
    generations_count: int,
    factory_version: str,
    diff_excerpt: str | None = None,
) -> str:
    """Generiert einen strukturierten PR-Body.

    Format soll für menschliche Reviewer schnell scannbar sein und gleichzeitig
    Telemetrie-Anker enthalten, an denen Loop 3 später Pattern erkennen kann.
    """
    delta_str = (
        f"{'+' if score_delta is not None and score_delta >= 0 else ''}{score_delta:.4f}"
        if score_delta is not None
        else "—"
    )
    score_str = f"{final_score:.4f}" if final_score is not None else "—"

    lines: list[str] = []
    lines.append("## forge auto-PR")
    lines.append("")
    lines.append(f"- **run_id:** `{run_id}`")
    lines.append(f"- **focus:** `{focus or 'manual'}`")
    lines.append(f"- **decision:** `{decision}`")
    lines.append(f"- **generations:** {generations_count}")
    lines.append(f"- **composite_score:** {score_str} (Δ {delta_str})")
    lines.append(f"- **total_cost:** ${total_cost_usd}")
    lines.append(f"- **factory_version:** `{factory_version}`")
    lines.append("")

    if files_changed:
        lines.append("### Files changed")
        lines.append("")
        for f in files_changed[:30]:
            lines.append(f"- `{f}`")
        if len(files_changed) > 30:
            lines.append(f"- _… and {len(files_changed) - 30} more_")
        lines.append("")

    if diff_excerpt:
        lines.append("<details><summary>Diff excerpt (first 4 KB)</summary>")
        lines.append("")
        lines.append("```diff")
        lines.append(diff_excerpt[:4000])
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append(
        "> Generated by [forge](https://github.com/anthropics/forge) — "
        "review carefully before merging. Auto-merge is categorically disabled in v1."
    )

    return "\n".join(lines)


_PR_URL_RE = re.compile(r"/pull/(\d+)")


def _extract_pr_number(url_or_output: str) -> int:
    """Extrahiert die PR-Nummer aus dem `gh pr create`-Output."""
    match = _PR_URL_RE.search(url_or_output)
    if match:
        return int(match.group(1))
    raise GitHubError(f"could not parse PR number from gh output: {url_or_output!r}")


# --- gh-Helpers --------------------------------------------------------


def gh_repo_default_branch(*, repo: Path, gh_bin: str = "gh") -> str:
    """Liefert den Default-Branch des Repos via `gh repo view --json defaultBranchRef`."""
    result = subprocess.run(
        [gh_bin, "repo", "view", "--json", "defaultBranchRef", "-q", ".defaultBranchRef.name"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(f"gh repo view failed: {result.stderr.strip()}")
    return result.stdout.strip() or "main"
