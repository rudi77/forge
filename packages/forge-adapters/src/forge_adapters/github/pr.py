"""PR-Erzeugung via `gh` CLI.

Pro Run mit `decision == pr_created` ruft der Caller `create_pr_for_run()`
auf. Die Funktion:

1. Pusht den Run-Branch zum Remote (`git push -u origin <branch>`)
2. Generiert einen strukturierten PR-Body (Score-Trend, betroffene Files,
   Run-Summary)
3. Ruft `gh pr create` mit Title, Body, Labels
4. Schreibt einen `PRCreated`-Event in den Store

Optional kann der Caller anschließend ``queue_auto_merge()`` aufrufen,
um GitHubs **server-seitiges** Auto-Merge für den frisch erzeugten PR
zu aktivieren. Wichtig: forge führt selbst keinen ``merge``-Subprozess
aus — die ``merge_pr``-Capability bleibt typed ``Literal[False]``. Wir
ziehen GitHubs Auto-Merge-Server-Feature an; den eigentlichen Merge
macht GitHub, nicht forge. Damit bleibt der Spec-Vertrag formal intakt.
Operatoren, die das nicht wollen, lassen ``--auto-merge`` weg.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    build_event,
)
from forge_core.store import EventStore

# Injizierbar für Tests; produktiv ``subprocess.run``.
SubprocessRunner = Callable[..., subprocess.CompletedProcess]

logger = logging.getLogger(__name__)


class GitHubError(RuntimeError):
    """gh CLI oder git-push-Fehler."""


@dataclass(frozen=True)
class PRCreationResult:
    pr_number: int
    url: str
    branch: str


def push_branch(
    *,
    repo: Path,
    branch: str,
    remote: str = "origin",
    attempts: int = 3,
    sleep: Callable[[float], None] = time.sleep,
    run_subprocess: SubprocessRunner = subprocess.run,
) -> None:
    """`git push -u <remote> <branch>` mit Retry bei transienten Fehlern.

    Netzwerk-Hiccups beim Push sind im 24/7-Betrieb der häufigste
    vermeidbare Run-Abbruch — bis zu ``attempts`` Versuche mit
    exponentiellem Backoff (2s, 4s, …). ``sleep``/``run_subprocess`` sind
    injizierbar, damit Tests ohne echte Wartezeit laufen.
    """
    last_stderr = ""
    for attempt in range(1, max(1, attempts) + 1):
        result = run_subprocess(
            ["git", "push", "-u", remote, branch],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode == 0:
            return
        last_stderr = (result.stderr or "").strip()
        if attempt < max(1, attempts):
            backoff = 2.0**attempt
            logger.warning(
                "git push %s/%s failed (attempt %d/%d), retrying in %.0fs: %s",
                remote,
                branch,
                attempt,
                attempts,
                backoff,
                last_stderr,
            )
            sleep(backoff)
    raise GitHubError(f"git push -u {remote} {branch} failed: {last_stderr}")


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
    plan_md: str | None = None,
) -> str:
    """Generiert einen strukturierten PR-Body.

    Format soll für menschliche Reviewer schnell scannbar sein und gleichzeitig
    Telemetrie-Anker enthalten, an denen Loop 3 später Pattern erkennen kann.

    Args:
        plan_md: Optionaler Markdown-Plan aus dem PlanProposed-Event. Wird
            als eigene Sektion in den Body eingebettet, sodass der Reviewer
            sieht, *was forge zu tun gedachte*, bevor er den Diff liest.
            Pläne >2 KB werden in ein ``<details>``-Element eingeklappt.
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

    if plan_md and plan_md.strip():
        plan_text = plan_md.strip()
        lines.append("### Plan")
        lines.append("")
        if len(plan_text) <= 2000:
            lines.append(plan_text)
        else:
            lines.append("<details><summary>Plan (full)</summary>")
            lines.append("")
            lines.append(plan_text)
            lines.append("")
            lines.append("</details>")
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


# --- Auto-Merge (Spec-Grauzone, dokumentiert) ---------------------------


MergeMethod = Literal["squash", "merge", "rebase"]


def repo_supports_auto_merge(
    *,
    repo: Path,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> bool:
    """Prüft via GraphQL, ob das Repo GitHubs Auto-Merge-Feature aktiviert hat.

    Achtung: ``gh repo view --json`` exposed ``autoMergeAllowed``
    NICHT (Stand gh 2.x); das Feld lebt nur in der GraphQL-API.
    Daher gehen wir direkt über ``gh api graphql``. Der Owner/Repo wird
    via ``gh repo view --json nameWithOwner`` ermittelt, damit wir nicht
    auf den Git-Remote-Parser angewiesen sind.

    Wird vor dem ersten ``queue_auto_merge``-Aufruf gerufen, damit ein
    Repo ohne aktiviertes Feature nicht mit kryptischer ``gh pr merge
    --auto``-Fehlermeldung crasht. Returns ``False`` wenn das Feature
    aus ist (Operator muss in Settings → General → "Allow auto-merge"
    einschalten); raised ``GitHubError`` bei gh-Fehlern.
    """
    slug_result = run_subprocess(
        [gh_bin, "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if slug_result.returncode != 0:
        raise GitHubError(
            f"gh repo view --json nameWithOwner failed (exit "
            f"{slug_result.returncode}): "
            f"{slug_result.stderr.strip() or '<no stderr>'}"
        )
    slug = slug_result.stdout.strip()
    if "/" not in slug:
        raise GitHubError(
            f"unexpected nameWithOwner shape: {slug!r}"
        )
    owner, name = slug.split("/", 1)

    query = (
        "query($owner: String!, $name: String!) { "
        "repository(owner: $owner, name: $name) { autoMergeAllowed } }"
    )
    cmd = [
        gh_bin, "api", "graphql",
        "-f", f"query={query}",
        "-F", f"owner={owner}",
        "-F", f"name={name}",
        "--jq", ".data.repository.autoMergeAllowed",
    ]
    result = run_subprocess(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"gh api graphql autoMergeAllowed failed (exit "
            f"{result.returncode}): {result.stderr.strip() or '<no stderr>'}"
        )
    return result.stdout.strip().lower() == "true"


def queue_auto_merge(
    *,
    repo: Path,
    pr_number: int,
    method: MergeMethod = "squash",
    delete_branch: bool = True,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> None:
    """Aktiviert GitHubs server-seitiges Auto-Merge für den PR.

    forge führt **nicht** selbst ``gh pr merge <N>`` synchron aus — das
    würde die ``merge_pr``-Capability (typed ``Literal[False]``)
    verletzen. Stattdessen queuen wir den Merge: ``gh pr merge --auto``
    sagt GitHub "merge automatisch sobald alle required Checks grün
    sind". Der Merge passiert server-seitig, asynchron, von GitHubs
    Bots — nicht von forge.

    Args:
        repo: Repository-Pfad (cwd für gh).
        pr_number: PR-Nummer aus ``create_pr_for_run`` Result.
        method: Squash (default) / merge / rebase.
        delete_branch: ``--delete-branch`` an gh durchreichen.
        gh_bin: gh-Binary-Pfad.
        run_subprocess: Injektabel für Tests.

    Raises:
        GitHubError: Wenn das Repo Auto-Merge nicht aktiviert hat oder
            der gh-Aufruf scheitert. Der Caller bricht in diesem Fall
            sauber ab; der PR bleibt offen für manuelles Mergen.
    """
    if not repo_supports_auto_merge(
        repo=repo, gh_bin=gh_bin, run_subprocess=run_subprocess
    ):
        raise GitHubError(
            "Repository has auto-merge disabled. Enable in Settings → "
            "General → 'Allow auto-merge', or run without --auto-merge."
        )

    method_flag = {
        "squash": "--squash",
        "merge": "--merge",
        "rebase": "--rebase",
    }[method]

    cmd = [
        gh_bin, "pr", "merge", str(pr_number),
        "--auto",
        method_flag,
    ]
    if delete_branch:
        cmd.append("--delete-branch")

    result = run_subprocess(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"gh pr merge --auto failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
