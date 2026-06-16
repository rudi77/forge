"""PR-Erzeugung via `gh` CLI.

Pro Run mit `decision == pr_created` ruft der Caller `create_pr_for_run()`
auf. Die Funktion:

1. Pusht den Run-Branch zum Remote (`git push -u origin <branch>`)
2. Generiert einen strukturierten PR-Body (Score-Trend, betroffene Files,
   Run-Summary)
3. Ruft `gh pr create` mit Title, Body, Labels
4. Schreibt einen `PRCreated`-Event in den Store

Es gibt zwei Merge-Wege:

1. ``queue_auto_merge()`` — GitHubs **server-seitiges** Auto-Merge; forge
   führt selbst keinen Merge-Subprozess aus, GitHub merged asynchron sobald
   die Checks grün sind. Spec-Grauzone (s.u.).
2. ``merge_pr()`` — forge **merged synchron selbst** (``gh pr merge``).
   Opt-in via ``capabilities.merge_pr`` und zusätzlich durch ein Agent-Review
   (verdict ``approve``) + grünen CI abgesichert. Das ist die bewusste Abkehr
   vom kategorischen v1-Ausschluss (Agent-Review-Merge). ``push_to_main`` /
   ``push_force`` bleiben hart verboten.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from forge_core.events import (
    EventKind,
    PRCreatedPayload,
    build_event,
)
from forge_core.store import EventStore

# Injizierbar für Tests; produktiv ``subprocess.run``.
SubprocessRunner = Callable[..., subprocess.CompletedProcess]


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
        "review carefully before merging. forge may merge this PR itself only "
        "after an agent review (verdict approve) with green CI, and only if the "
        "`merge_pr` capability is opt-in enabled."
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


# --- Agent-Review-Merge: PR-Kontext, Review-Post, direkter Merge ---------
#
# Anders als die GitHub-Auto-Merge-Grauzone (``queue_auto_merge``) führt
# forge hier den Merge **selbst synchron** aus (``gh pr merge <N>`` ohne
# ``--auto``). Das ist opt-in via ``capabilities.merge_pr`` und zusätzlich
# durch ein Agent-Review (verdict ``approve``) + grünen CI abgesichert. Diese
# Funktionen sind das dünne gh-CLI-Wrapper-Layer; die Urteils- und
# Entscheidungslogik liegt darüber (forge-execute / forge-cli).


@dataclass(frozen=True)
class PRMetadata:
    """Schlanke Sicht auf einen offenen PR (aus ``gh pr view --json``)."""

    number: int
    title: str
    body: str
    state: str
    base_branch: str
    head_branch: str
    ci_status: str
    """Aggregiert via :func:`summarize_ci` — pass|fail|pending|none|unknown."""
    mergeable: str
    """GitHubs ``mergeable``: MERGEABLE | CONFLICTING | UNKNOWN."""


@dataclass(frozen=True)
class MergeResult:
    merged: bool
    merger: str
    method: str


def gh_current_login(
    *,
    repo: Path,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> str:
    """Authentifizierten gh-Login (für das ``merger``-Feld). Fallback ``forge``."""
    result = run_subprocess(
        [gh_bin, "api", "user", "--jq", ".login"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return "forge"
    return result.stdout.strip() or "forge"


def fetch_pr_diff(
    *,
    repo: Path,
    pr_number: int,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> str:
    """Roher Unified-Diff eines PRs via ``gh pr diff <N>``."""
    result = run_subprocess(
        [gh_bin, "pr", "diff", str(pr_number)],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"gh pr diff {pr_number} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    return result.stdout


_PR_VIEW_FIELDS = "number,title,body,state,baseRefName,headRefName,mergeable,statusCheckRollup"


def fetch_pr_metadata(
    *,
    repo: Path,
    pr_number: int,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> PRMetadata:
    """PR-Metadaten + aggregierter CI-Status via ``gh pr view --json``."""
    result = run_subprocess(
        [gh_bin, "pr", "view", str(pr_number), "--json", _PR_VIEW_FIELDS],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise GitHubError(
            f"gh pr view {pr_number} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise GitHubError(f"could not parse gh pr view JSON: {exc}") from exc

    return PRMetadata(
        number=int(data.get("number", pr_number)),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        state=str(data.get("state") or "").upper(),
        base_branch=str(data.get("baseRefName") or "main"),
        head_branch=str(data.get("headRefName") or ""),
        ci_status=summarize_ci(data.get("statusCheckRollup") or []),
        mergeable=str(data.get("mergeable") or "UNKNOWN").upper(),
    )


def fetch_pr_head_committed_at(
    *,
    repo: Path,
    pr_number: int,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> datetime | None:
    """Zeitstempel des Head-Commits eines offenen PRs via ``gh pr view --json commits``.

    Fail-open: gibt ``None`` zurück, wenn gh fehlschlägt, die Ausgabe nicht
    parsebar oder leer ist — ein transienter gh-Schluckauf darf den
    Conductor-Tick nicht wedgen (der Caller fällt dann aufs alte
    ``review_done``-Verhalten zurück, statt das Re-Review zu blockieren). ``gh
    pr view --json commits`` liefert die Commits **oldest-first**; das letzte
    Element ist der Head-Commit, dessen ``committedDate`` (ISO8601, UTC ``Z``)
    als tz-aware ``datetime`` zurückgegeben wird.
    """
    try:
        result = run_subprocess(
            [gh_bin, "pr", "view", str(pr_number), "--json", "commits"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    commits = data.get("commits") or []
    if not commits:
        return None
    raw = commits[-1].get("committedDate")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


# Conclusions/States, die GitHubs Checks als Misserfolg melden.
_CI_FAIL = {"FAILURE", "ERROR", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE"}
# Erfolgreiche Endzustände (neutral/skipped gelten als "nicht blockierend").
_CI_PASS = {"SUCCESS", "NEUTRAL", "SKIPPED", "EXPECTED"}


def summarize_ci(rollup: list[dict[str, Any]]) -> str:
    """Aggregiert GitHubs ``statusCheckRollup`` zu pass|fail|pending|none|unknown.

    Rein, ohne I/O. Der Rollup mischt zwei Shapes: CheckRun
    (``status``/``conclusion``) und StatusContext (``state``). Regeln:

    - leer                       → ``none`` (keine Checks konfiguriert)
    - irgendein Check fehlgeschlagen → ``fail`` (überstimmt alles)
    - irgendein Check noch nicht fertig → ``pending``
    - alle erfolgreich           → ``pass``
    - sonst                      → ``unknown``
    """
    if not rollup:
        return "none"

    saw_pending = False
    saw_pass = False
    for check in rollup:
        conclusion = str(check.get("conclusion") or "").upper()
        status = str(check.get("status") or "").upper()
        state = str(check.get("state") or "").upper()

        # StatusContext: nur `state`.
        if state and not status and not conclusion:
            if state in _CI_FAIL:
                return "fail"
            if state in {"PENDING"}:
                saw_pending = True
            elif state in _CI_PASS:
                saw_pass = True
            else:
                saw_pending = True
            continue

        # CheckRun: `status` (QUEUED/IN_PROGRESS/COMPLETED) + `conclusion`.
        if status and status != "COMPLETED":
            saw_pending = True
            continue
        if conclusion in _CI_FAIL:
            return "fail"
        if conclusion in _CI_PASS or conclusion:
            saw_pass = True
        else:
            saw_pending = True

    if saw_pending:
        return "pending"
    if saw_pass:
        return "pass"
    return "unknown"


def post_pr_review(
    *,
    repo: Path,
    pr_number: int,
    approve: bool,
    body: str,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> None:
    """Postet ein GitHub-Review (``gh pr review --approve|--request-changes``).

    GitHub erlaubt kein ``--approve`` auf den eigenen PR (wenn forge unter
    demselben Account läuft); in dem Fall fällt der gh-Aufruf mit Fehler aus
    und der Caller behandelt das als nicht-fatal (``GitHubError`` wird
    geworfen, der Merge-Pfad entscheidet selbst).
    """
    flag = "--approve" if approve else "--request-changes"
    cmd = [gh_bin, "pr", "review", str(pr_number), flag, "--body", body]
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
            f"gh pr review {pr_number} {flag} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )


def merge_pr(
    *,
    repo: Path,
    pr_number: int,
    method: MergeMethod = "squash",
    delete_branch: bool = True,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> MergeResult:
    """Merged einen PR **synchron** via ``gh pr merge <N>`` (ohne ``--auto``).

    Im Gegensatz zu :func:`queue_auto_merge` führt forge den Merge selbst
    aus. Caller MUSS vorher sichergestellt haben:

    - ``capabilities.merge_pr`` ist opt-in aktiviert,
    - ein Agent-Review hat ``approve`` geliefert,
    - der CI-Status ist grün (:func:`summarize_ci` == ``pass``/``none``).

    Diese Funktion prüft das nicht — sie ist der dünne Effekt. Die
    Entscheidung liegt in ``forge_execute.pr_review`` / ``forge review-pr``.

    Raises:
        GitHubError: Wenn der Merge scheitert (Konflikt, fehlende Rechte,
            nicht-grüne required Checks GitHub-seitig). Der PR bleibt dann
            offen.
    """
    method_flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}[method]
    cmd = [gh_bin, "pr", "merge", str(pr_number), method_flag]
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
            f"gh pr merge {pr_number} failed (exit {result.returncode}): "
            f"{result.stderr.strip() or '<no stderr>'}"
        )
    merger = gh_current_login(repo=repo, gh_bin=gh_bin, run_subprocess=run_subprocess)
    return MergeResult(merged=True, merger=merger, method=method)


def create_release(
    *,
    repo: Path,
    tag: str,
    title: str,
    notes: str | None = None,
    generate_notes: bool = True,
    gh_bin: str = "gh",
    run_subprocess: SubprocessRunner = subprocess.run,
) -> str:
    """Erzeugt einen Tag + GitHub-Release via ``gh release create`` (B2).

    Deterministischer forge-Effekt für die ``release``-Stage — opt-in über
    ``capabilities.create_release``. ``gh release create`` legt Tag UND Release
    in einem Aufruf an (kein manuelles ``git push``/``--force`` — ein Release
    schreibt einen neuen Ref, fasst weder main noch force an).

    **Idempotent:** existiert der Tag schon (Re-Dispatch desselben Items),
    wird die bestehende Release-URL zurückgegeben statt zu scheitern — sonst
    re-dispatcht die in-place release-Stage jeden Tick erfolglos.

    Gibt die Release-URL zurück (``gh release create`` schreibt sie nach stdout).
    """
    existing = _release_url_if_exists(
        repo=repo, tag=tag, gh_bin=gh_bin, run_subprocess=run_subprocess
    )
    if existing is not None:
        return existing

    cmd = [gh_bin, "release", "create", tag, "--title", title]
    if generate_notes:
        cmd.append("--generate-notes")
    if notes:
        cmd += ["--notes", notes]
    result = run_subprocess(
        cmd,
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Race/Re-Dispatch: Tag zwischenzeitlich angelegt → als Erfolg werten.
        if "already exists" in stderr.lower():
            existing = _release_url_if_exists(
                repo=repo, tag=tag, gh_bin=gh_bin, run_subprocess=run_subprocess
            )
            if existing is not None:
                return existing
        raise GitHubError(
            f"gh release create {tag} failed (exit {result.returncode}): "
            f"{stderr or '<no stderr>'}"
        )
    return result.stdout.strip()


def _release_url_if_exists(
    *,
    repo: Path,
    tag: str,
    gh_bin: str,
    run_subprocess: SubprocessRunner,
) -> str | None:
    """Release-URL eines bestehenden Tags via ``gh release view``, oder ``None``."""
    result = run_subprocess(
        [gh_bin, "release", "view", tag, "--json", "url"],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    url = data.get("url")
    return str(url) if url else ""
