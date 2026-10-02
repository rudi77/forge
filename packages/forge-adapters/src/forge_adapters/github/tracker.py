"""GitHub als :class:`~forge_adapters.base.WorkTracker` (Issues + Labels).

Dünne Klasse über die bestehenden gh-CLI-Funktionen (``board.py``) plus die
neuen Tracker-Operationen (Kommentar, Close, Labels anlegen, Issues anlegen,
Body-Suche). Gleiches Muster wie überall im Adapter: argv-Listen (nie
``shell=True``), ``run_subprocess`` injizierbar für Tests.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from forge_core.spec import BoardConfig
from forge_core.tracking import NewWorkItem, ReadyIssue

from forge_adapters.base import CloseReason, LabelReport
from forge_adapters.github.board import (
    BoardError,
    SubprocessRunner,
    list_ready_items,
    list_stage_items,
    set_issue_stage_label,
)

_ISSUE_URL_RE = re.compile(r"/issues/(\d+)")

# Farben für die forge-eigenen Labels (rein kosmetisch).
_LABEL_COLORS = {
    "forge:blocked": "d73a4a",
    "forge:proposed": "c5def5",
    "forge:generated": "ededed",
    "forge:epic": "5319e7",
}
_DEFAULT_LABEL_COLOR = "0e8a16"


class GitHubTracker:
    """GitHub-Issues als Work-Tracker für ``owner/repo``."""

    provider = "github"

    def __init__(
        self,
        *,
        owner: str,
        repo: str,
        repo_root: Path | None = None,
        gh_bin: str = "gh",
        run_subprocess: SubprocessRunner = subprocess.run,
    ) -> None:
        self.owner = owner
        self.repo = repo
        self.repo_root = repo_root
        self.gh_bin = gh_bin
        self._run = run_subprocess

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    # --- Lesen ------------------------------------------------------------

    def list_ready_items(self, board: BoardConfig) -> list[ReadyIssue]:
        return list_ready_items(
            board,
            repo_owner=self.owner,
            repo_name=self.repo,
            gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def list_stage_items(
        self, *, stage_labels: list[str], state: str = "open"
    ) -> list[ReadyIssue]:
        return list_stage_items(
            repo_owner=self.owner,
            repo_name=self.repo,
            stage_labels=stage_labels,
            state=state,
            gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def get_items(self, numbers: list[int]) -> list[ReadyIssue]:
        out: list[ReadyIssue] = []
        for n in numbers:
            data = self._gh_json(
                [
                    "issue", "view", str(n),
                    "--repo", self.slug,
                    "--json", "number,title,body,labels,state,url",
                ],
                what=f"gh issue view #{n}",
            )
            out.append(_issue_from_json(data, project_status="(override)"))
        return out

    def search_items(self, text: str) -> list[ReadyIssue]:
        data = self._gh_json(
            [
                "issue", "list",
                "--repo", self.slug,
                "--state", "all",
                "--limit", "50",
                "--search", f'"{text}" in:body',
                "--json", "number,title,body,labels,url",
            ],
            what="gh issue list --search",
        )
        # Die GitHub-Suche ist unscharf (Tokenizer) → exakt nachfiltern.
        return [
            _issue_from_json(item)
            for item in (data or [])
            if text in (item.get("body") or "")
        ]

    # --- Schreiben ----------------------------------------------------------

    def set_stage(self, *, number: int, add: str, remove: str | None = None) -> None:
        set_issue_stage_label(
            issue_number=number,
            repo_owner=self.owner,
            repo_name=self.repo,
            add=add,
            remove=remove,
            gh_bin=self.gh_bin,
            run_subprocess=self._run,
        )

    def comment(self, *, number: int, body: str) -> None:
        self._gh(
            ["issue", "comment", str(number), "--repo", self.slug, "--body", body],
            what=f"gh issue comment #{number}",
        )

    def close(self, *, number: int, reason: CloseReason = "not planned") -> None:
        self._gh(
            ["issue", "close", str(number), "--repo", self.slug, "--reason", reason],
            what=f"gh issue close #{number}",
        )

    def ensure_labels(self, labels: list[str], *, create: bool = False) -> LabelReport:
        data = self._gh_json(
            ["label", "list", "--repo", self.slug, "--limit", "1000", "--json", "name"],
            what="gh label list",
        )
        existing = {str(item.get("name", "")) for item in (data or [])}
        present = [lbl for lbl in labels if lbl in existing]
        missing = [lbl for lbl in labels if lbl not in existing]
        created: list[str] = []
        if create:
            for lbl in missing:
                self._gh(
                    [
                        "label", "create", lbl,
                        "--repo", self.slug,
                        "--color", _LABEL_COLORS.get(lbl, _DEFAULT_LABEL_COLOR),
                        "--description", "forge factory stage/marker",
                    ],
                    what=f"gh label create {lbl}",
                )
                created.append(lbl)
            missing = []
        return LabelReport(present=present, missing=missing, created=created)

    def create_item(self, item: NewWorkItem) -> ReadyIssue:
        labels = [*item.labels, f"type:{item.kind}"]
        self.ensure_labels(labels, create=True)
        body = item.body
        if item.parent is not None and f"#{item.parent}" not in body:
            body = f"{body}\n\nParent: #{item.parent}"
        cmd = ["issue", "create", "--repo", self.slug, "--title", item.title, "--body", body]
        for lbl in labels:
            cmd += ["--label", lbl]
        result = self._gh(cmd, what="gh issue create")
        url = (result.stdout or "").strip().splitlines()[-1] if result.stdout else ""
        match = _ISSUE_URL_RE.search(url)
        if not match:
            raise BoardError(f"could not parse issue number from gh output: {url!r}")
        number = int(match.group(1))
        if item.parent is not None:
            self._link_sub_issue(parent=item.parent, child=number)
        return ReadyIssue(
            number=number,
            title=item.title,
            body=body,
            labels=labels,
            project_status="",
            url=url,
            kind=item.kind,
            parent=item.parent,
        )

    # --- Internals ----------------------------------------------------------

    def _link_sub_issue(self, *, parent: int, child: int) -> None:
        """Best-effort native Sub-Issue-Verknüpfung (REST braucht die interne
        Issue-ID, nicht die Nummer). Scheitert sie (ältere GHES, fehlende
        Rechte), bleibt die ``Parent: #N``-Zeile im Body die Quelle."""
        try:
            res = self._run(
                [self.gh_bin, "api", f"repos/{self.slug}/issues/{child}", "--jq", ".id"],
                **_SUBPROCESS_KW,
            )
            if res.returncode != 0 or not (res.stdout or "").strip():
                return
            self._run(
                [
                    self.gh_bin, "api", "-X", "POST",
                    f"repos/{self.slug}/issues/{parent}/sub_issues",
                    "-F", f"sub_issue_id={res.stdout.strip()}",
                ],
                **_SUBPROCESS_KW,
            )
        except OSError:
            return

    def _gh(self, args: list[str], *, what: str) -> subprocess.CompletedProcess:
        kwargs = dict(_SUBPROCESS_KW)
        if self.repo_root is not None:
            kwargs["cwd"] = str(self.repo_root)
        result = self._run([self.gh_bin, *args], **kwargs)
        if result.returncode != 0:
            raise BoardError(
                f"{what} failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )
        return result

    def _gh_json(self, args: list[str], *, what: str):
        result = self._gh(args, what=what)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise BoardError(f"{what} returned invalid JSON: {exc}") from exc


_SUBPROCESS_KW = {
    "capture_output": True,
    "text": True,
    "encoding": "utf-8",
    "errors": "replace",
}


def _issue_from_json(data: dict, *, project_status: str = "") -> ReadyIssue:
    labels_raw = data.get("labels") or []
    labels = [
        str(entry.get("name", "")) for entry in labels_raw if isinstance(entry, dict)
    ]
    return ReadyIssue(
        number=int(data.get("number", 0)),
        title=str(data.get("title") or ""),
        body=str(data.get("body") or ""),
        labels=[lbl for lbl in labels if lbl],
        project_status=project_status,
        url=str(data.get("url") or ""),
    )
