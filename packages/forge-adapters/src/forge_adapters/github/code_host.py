"""GitHub als :class:`~forge_adapters.base.CodeHost` (PRs, Checks, Releases).

Delegiert an die bestehenden gh-Wrapper in ``pr.py`` und ergänzt, was der
Conductor neu braucht: Push auf einen bestehenden Branch (Nacharbeit, L1),
CI-Fehlerauszug (L2), CI-Status eines Branches (A: CI rot auf ``main``) und
die Liste offener PRs (G: Geschwister nachziehen).

Der PR-Erzeugungspfad emittiert hier **kein** Event mehr — das ``PRCreated``
schreibt der anbieter-neutrale Caller (``forge_cli.run``).
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from forge_adapters.base import (
    MergeMethod,
    MergeResult,
    OpenChange,
    PRCreationResult,
    PRMetadata,
    git_push_argv,
)
from forge_adapters.github.pr import (
    GitHubError,
    SubprocessRunner,
    _extract_pr_number,
    create_release,
    fetch_pr_diff,
    fetch_pr_head_committed_at,
    fetch_pr_metadata,
    merge_pr,
    post_pr_review,
    queue_auto_merge,
    summarize_ci,
)

_RUN_ID_RE = re.compile(r"/actions/runs/(\d+)")

_SUBPROCESS_KW = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


class GitHubCodeHost:
    """GitHub-PRs für das Repo unter ``repo_root`` (gh nutzt dessen Remote)."""

    provider = "github"

    def __init__(
        self,
        *,
        repo_root: Path,
        gh_bin: str = "gh",
        run_subprocess: SubprocessRunner = subprocess.run,
    ) -> None:
        self.repo_root = repo_root
        self.gh_bin = gh_bin
        self._run = run_subprocess

    # --- Branch + PR ----------------------------------------------------

    def push_branch(
        self, *, branch: str, remote: str = "origin", target: str | None = None
    ) -> None:
        argv = git_push_argv(branch=branch, remote=remote, target=target)
        result = self._run(argv, cwd=str(self.repo_root), **_SUBPROCESS_KW)
        if result.returncode != 0:
            raise GitHubError(f"{' '.join(argv)} failed: {(result.stderr or '').strip()}")

    def open_change(
        self,
        *,
        branch: str,
        title: str,
        body: str,
        base: str = "main",
        labels: list[str] | None = None,
        draft: bool = False,
        push: bool = True,
    ) -> PRCreationResult:
        if push:
            self.push_branch(branch=branch)
        cmd = [
            self.gh_bin, "pr", "create",
            "--base", base,
            "--head", branch,
            "--title", title,
            "--body", body,
        ]
        if draft:
            cmd.append("--draft")
        for lbl in labels or ["forge:auto"]:
            cmd += ["--label", lbl]
        result = self._run(cmd, cwd=str(self.repo_root), **_SUBPROCESS_KW)
        if result.returncode != 0:
            raise GitHubError(
                f"gh pr create failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip()}"
            )
        url = (result.stdout or "").strip().splitlines()[-1]
        return PRCreationResult(pr_number=_extract_pr_number(url), url=url, branch=branch)

    def queue_auto_merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> None:
        queue_auto_merge(
            repo=self.repo_root,
            pr_number=pr_number,
            method=method,
            delete_branch=delete_branch,
            gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def fetch_metadata(self, pr_number: int) -> PRMetadata:
        return fetch_pr_metadata(
            repo=self.repo_root, pr_number=pr_number, gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def fetch_diff(self, pr_number: int) -> str:
        return fetch_pr_diff(
            repo=self.repo_root, pr_number=pr_number, gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def head_committed_at(self, pr_number: int) -> datetime | None:
        return fetch_pr_head_committed_at(
            repo=self.repo_root, pr_number=pr_number, gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def post_review(self, *, pr_number: int, approve: bool, body: str) -> None:
        post_pr_review(
            repo=self.repo_root, pr_number=pr_number, approve=approve, body=body,
            gh_bin=self.gh_bin, run_subprocess=self._run,
        )

    def merge(
        self, *, pr_number: int, method: MergeMethod = "squash", delete_branch: bool = True
    ) -> MergeResult:
        return merge_pr(
            repo=self.repo_root, pr_number=pr_number, method=method,
            delete_branch=delete_branch, gh_bin=self.gh_bin, run_subprocess=self._run,
        )

    def create_release(
        self, *, tag: str, title: str, notes: str | None = None, target: str | None = None
    ) -> str:
        return create_release(
            repo=self.repo_root,
            tag=tag,
            title=title,
            notes=notes,
            target=target,
            # Eigene Notes (Changelog) ersetzen GitHubs generierte.
            generate_notes=notes is None,
            gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    # --- CI ----------------------------------------------------------------

    def ci_failure_summary(self, pr_number: int, *, max_chars: int = 8000) -> str:
        """Rote Checks + ``gh run view --log-failed``-Auszug. Fail-open."""
        try:
            res = self._run(
                [self.gh_bin, "pr", "view", str(pr_number), "--json", "statusCheckRollup"],
                cwd=str(self.repo_root),
                **_SUBPROCESS_KW,
            )
            if res.returncode != 0:
                return ""
            rollup = (json.loads(res.stdout or "{}") or {}).get("statusCheckRollup") or []
        except (OSError, json.JSONDecodeError):
            return ""
        failed = [c for c in rollup if summarize_ci([c]) == "fail"]
        if not failed:
            return ""
        parts = ["Failing checks:"]
        run_ids: list[str] = []
        for check in failed:
            name = check.get("name") or check.get("context") or "?"
            url = str(check.get("detailsUrl") or check.get("targetUrl") or "")
            parts.append(f"- {name} {url}".rstrip())
            m = _RUN_ID_RE.search(url)
            if m and m.group(1) not in run_ids:
                run_ids.append(m.group(1))
        for run_id in run_ids[:2]:
            try:
                log = self._run(
                    [self.gh_bin, "run", "view", run_id, "--log-failed"],
                    cwd=str(self.repo_root),
                    **_SUBPROCESS_KW,
                )
            except OSError:
                continue
            if log.returncode == 0 and log.stdout:
                parts.append(f"\n--- log (run {run_id}, tail) ---")
                parts.append(log.stdout[-max_chars:])
        return "\n".join(parts)[-max_chars:]

    def ref_ci_status(self, ref: str) -> str:
        try:
            res = self._run(
                [self.gh_bin, "api", f"repos/:owner/:repo/commits/{ref}/check-runs"],
                cwd=str(self.repo_root),
                **_SUBPROCESS_KW,
            )
            if res.returncode != 0:
                return "unknown"
            runs = (json.loads(res.stdout or "{}") or {}).get("check_runs") or []
        except (OSError, json.JSONDecodeError):
            return "unknown"
        rollup = [
            {
                "status": str(r.get("status") or "").upper(),
                "conclusion": str(r.get("conclusion") or "").upper(),
            }
            for r in runs
        ]
        return summarize_ci(rollup)

    def list_open_changes(self) -> list[OpenChange]:
        res = self._run(
            [
                self.gh_bin, "pr", "list", "--state", "open", "--limit", "200",
                "--json", "number,headRefName,baseRefName",
            ],
            cwd=str(self.repo_root),
            **_SUBPROCESS_KW,
        )
        if res.returncode != 0:
            raise GitHubError(f"gh pr list failed: {(res.stderr or '').strip()}")
        try:
            data = json.loads(res.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise GitHubError(f"gh pr list returned invalid JSON: {exc}") from exc
        return [
            OpenChange(
                number=int(d["number"]),
                head_branch=str(d.get("headRefName") or ""),
                base_branch=str(d.get("baseRefName") or ""),
            )
            for d in data
        ]
