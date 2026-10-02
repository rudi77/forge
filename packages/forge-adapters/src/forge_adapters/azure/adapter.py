"""Azure DevOps als ``WorkTracker`` (Boards) + ``CodeHost`` (Repos/Pipelines).

Zugriff über die ``az``-CLI mit der ``azure-devops``-Extension — gleiches Muster
wie der GitHub-Adapter mit ``gh``: argv-Listen (nie ``shell=True``),
``run_subprocess`` injizierbar, Tests laufen gegen einen ``az``-Simulator.
Auth: ``AZURE_DEVOPS_EXT_PAT`` oder ``az login`` — nie in der Spec.

Abbildung (Roadmap §P):

* Stage-Labels → Work-Item-**Tags** (``forge:<stage>``); Tags existieren
  implizit, ``ensure_labels`` ist daher immer erfüllt.
* Work-Item-Body → ``System.Description`` (HTML). forge schreibt Text als HTML
  (Zeilenumbrüche → ``<br>``) und liest ihn als Text zurück, damit
  ``Depends-On:``/``Touches:``/Fingerprint-Zeilen parsebar bleiben.
* PR-Review → Reviewer-Vote (``approve`` / ``wait-for-author``) + Kommentar-
  Thread via ``az devops invoke`` (die CLI hat keinen PR-Kommentar-Befehl).
* CI → Build-/Status-Policies des PRs (``az repos pr policy list``).
* Release → annotierter Git-Tag (Azure Repos kennt kein Release-Objekt).
* PR-Diff / Head-Commit-Zeit → lokal über git (die CLI liefert beides nicht).
"""

from __future__ import annotations

import html
import json
import re
import subprocess
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from forge_core.spec import AzureDevOpsConfig, BoardConfig
from forge_core.tracking import NewWorkItem, ReadyIssue

from forge_adapters.base import (
    CloseReason,
    CodeHostError,
    LabelReport,
    MergeMethod,
    MergeResult,
    OpenChange,
    PRCreationResult,
    PRMetadata,
    TrackerError,
    git_push_argv,
)

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

_KW = {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace"}

# Azure begrenzt PR-Beschreibungen auf 4000 Zeichen.
_PR_DESCRIPTION_LIMIT = 4000

_FIELDS = (
    "[System.Id], [System.Title], [System.Description], [System.Tags], "
    "[System.State], [System.WorkItemType]"
)

# States, die über alle Prozess-Templates (Agile/Scrum/CMMI/Basic) "zu" bedeuten.
_CLOSED_STATES = ("Closed", "Done", "Removed", "Resolved")


class AzureDevOpsError(TrackerError):
    """``az``-Aufruf für Boards fehlgeschlagen."""


class AzureReposError(CodeHostError):
    """``az``-/git-Aufruf für Repos fehlgeschlagen."""


# --- Text <-> HTML ------------------------------------------------------------


def text_to_html(text: str) -> str:
    """Plaintext/Markdown → minimal HTML (escape + ``<br>``)."""
    return html.escape(text).replace("\n", "<br>")


_TAG_RE = re.compile(r"<[^>]+>")
_BREAK_RE = re.compile(r"<\s*(br|/p|/div|/li|/h\d)\s*/?\s*>", re.IGNORECASE)


def html_to_text(value: str) -> str:
    """Azure-``System.Description`` (HTML) → Text mit Zeilenumbrüchen."""
    if not value:
        return ""
    text = _BREAK_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def split_tags(value: str | None) -> list[str]:
    return [t.strip() for t in (value or "").split(";") if t.strip()]


def join_tags(tags: list[str]) -> str:
    return "; ".join(tags)


def _wiql_str(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


# --- Basis ----------------------------------------------------------------------


class _AzBase:
    def __init__(self, *, config: AzureDevOpsConfig, run_subprocess: SubprocessRunner,
                 az_bin: str = "az") -> None:
        self.config = config
        self.az_bin = az_bin
        self._run = run_subprocess

    def _org(self) -> list[str]:
        return ["--org", self.config.org_url, "--detect", "false"]

    def _proj(self) -> list[str]:
        return ["--project", self.config.project]

    def _az(self, args: list[str], *, what: str, error=TrackerError,
            cwd: Path | None = None) -> subprocess.CompletedProcess:
        kw = dict(_KW)
        if cwd is not None:
            kw["cwd"] = str(cwd)
        result = self._run([self.az_bin, *args], **kw)
        if result.returncode != 0:
            raise error(
                f"{what} failed (exit {result.returncode}): "
                f"{(result.stderr or '').strip() or '<no stderr>'}"
            )
        return result

    def _az_json(self, args: list[str], *, what: str, error=TrackerError):
        result = self._az([*args, "-o", "json"], what=what, error=error)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError as exc:
            raise error(f"{what} returned invalid JSON: {exc}") from exc


# --- Tracker -----------------------------------------------------------------------


class AzureBoardsTracker(_AzBase):
    """Azure-Boards-Work-Items als forge-Work-Tracker."""

    provider = "azure_devops"

    def __init__(self, *, config: AzureDevOpsConfig,
                 run_subprocess: SubprocessRunner = subprocess.run,
                 az_bin: str = "az") -> None:
        super().__init__(config=config, run_subprocess=run_subprocess, az_bin=az_bin)
        self._kind_by_type = {v: k for k, v in config.work_item_types.items()}

    # --- Lesen ------------------------------------------------------------

    def list_ready_items(self, board: BoardConfig) -> list[ReadyIssue]:
        clauses = [f"[System.State] = {_wiql_str(board.filter_status)}"]
        clauses += [f"[System.Tags] CONTAINS {_wiql_str(lbl)}" for lbl in board.filter_labels]
        items = self._query(" AND ".join(clauses))
        # Idempotenz wie bei GitHub: Items, die schon eine forge-Stage tragen,
        # sind in Arbeit und gehören dem Conductor.
        return [i for i in items if not any(t.startswith("forge:") for t in i.labels)]

    def list_stage_items(self, *, stage_labels: list[str], state: str = "open") -> list[ReadyIssue]:
        if not stage_labels:
            return []
        tags = " OR ".join(f"[System.Tags] CONTAINS {_wiql_str(lbl)}" for lbl in stage_labels)
        clause = f"({tags})"
        closed = ", ".join(_wiql_str(s) for s in {*_CLOSED_STATES, self.config.closed_state})
        if state == "open":
            clause += f" AND [System.State] NOT IN ({closed})"
        elif state == "closed":
            clause += f" AND [System.State] IN ({closed})"
        wanted = set(stage_labels)
        return [i for i in self._query(clause) if wanted.intersection(i.labels)]

    def get_items(self, numbers: list[int]) -> list[ReadyIssue]:
        out = []
        for n in numbers:
            data = self._az_json(
                ["boards", "work-item", "show", "--id", str(n), *self._org()],
                what=f"az boards work-item show {n}",
                error=AzureDevOpsError,
            )
            out.append(self._item(data, project_status="(override)"))
        return out

    def search_items(self, text: str) -> list[ReadyIssue]:
        # Description ist ein HTML-Langtextfeld → "Contains Words"; exakt nachfiltern.
        words = re.sub(r"[^\w:.-]+", " ", text).strip()
        items = self._query(f"[System.Description] CONTAINS WORDS {_wiql_str(words)}")
        return [i for i in items if text in i.body]

    # --- Schreiben ----------------------------------------------------------

    def set_stage(self, *, number: int, add: str, remove: str | None = None) -> None:
        (item,) = self.get_items([number])
        tags = [t for t in item.labels if t != remove]
        if add not in tags:
            tags.append(add)
        if tags == item.labels:
            return
        self._update(number, ["--fields", f"System.Tags={join_tags(tags)}"])

    def comment(self, *, number: int, body: str) -> None:
        self._update(number, ["--discussion", text_to_html(body)])

    def close(self, *, number: int, reason: CloseReason = "not planned") -> None:
        self._update(
            number,
            ["--state", self.config.closed_state,
             "--discussion", text_to_html(f"forge: closed ({reason})")],
        )

    def ensure_labels(self, labels: list[str], *, create: bool = False) -> LabelReport:
        # Tags entstehen beim ersten Setzen — es fehlt nie etwas. Ein Lesezugriff
        # verifiziert trotzdem Erreichbarkeit/Rechte (doctor --board).
        self._query("[System.Id] < 0")
        return LabelReport(present=list(labels), missing=[], created=[])

    def create_item(self, item: NewWorkItem) -> ReadyIssue:
        wit = self.config.work_item_types.get(item.kind, "Task")
        data = self._az_json(
            [
                "boards", "work-item", "create",
                "--type", wit,
                "--title", item.title,
                "--description", text_to_html(item.body),
                "--fields", f"System.Tags={join_tags(list(item.labels))}",
                *self._org(), *self._proj(),
            ],
            what="az boards work-item create",
            error=AzureDevOpsError,
        )
        created = self._item(data)
        if item.parent is not None:
            self._az(
                ["boards", "work-item", "relation", "add", "--id", str(created.number),
                 "--relation-type", "parent", "--target-id", str(item.parent), *self._org()],
                what="az boards work-item relation add",
                error=AzureDevOpsError,
            )
        return ReadyIssue(
            number=created.number, title=created.title, body=item.body,
            labels=created.labels, project_status=created.project_status, url=created.url,
            kind=item.kind, parent=item.parent,
        )

    # --- Internals ------------------------------------------------------------

    def _query(self, where: str) -> list[ReadyIssue]:
        wiql = (
            f"SELECT {_FIELDS} FROM WorkItems WHERE [System.TeamProject] = @project "
            f"AND {where} ORDER BY [System.Id]"
        )
        data = self._az_json(
            ["boards", "query", "--wiql", wiql, *self._org(), *self._proj()],
            what="az boards query",
            error=AzureDevOpsError,
        )
        return sorted((self._item(d) for d in data or []), key=lambda i: i.number)

    def _update(self, number: int, args: list[str]) -> None:
        self._az(
            ["boards", "work-item", "update", "--id", str(number), *args, *self._org(),
             "-o", "none"],
            what=f"az boards work-item update {number}",
            error=AzureDevOpsError,
        )

    def _item(self, data: dict, *, project_status: str | None = None) -> ReadyIssue:
        fields = data.get("fields") or {}
        number = int(data.get("id") or fields.get("System.Id") or 0)
        links = (data.get("_links") or {}).get("html") or {}
        url = links.get("href") or (
            f"{self.config.org_url}/{self.config.project}/_workitems/edit/{number}"
        )
        wit = str(fields.get("System.WorkItemType") or "")
        return ReadyIssue(
            number=number,
            title=str(fields.get("System.Title") or ""),
            body=html_to_text(str(fields.get("System.Description") or "")),
            labels=split_tags(fields.get("System.Tags")),
            project_status=(
                project_status if project_status is not None
                else str(fields.get("System.State") or "")
            ),
            url=url,
            kind=self._kind_by_type.get(wit),  # type: ignore[arg-type]
        )


# --- Code-Host --------------------------------------------------------------------


_PR_STATE = {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}
_MERGEABLE = {"succeeded": "MERGEABLE", "conflicts": "CONFLICTING"}
_CI_POLICY_TYPES = {"build", "status"}


def summarize_policies(evaluations: list[dict]) -> str:
    """Build-/Status-Policy-Evaluations → pass|fail|pending|none.

    Reviewer-/Kommentar-Policies zählen bewusst NICHT (die wären bis zur
    Freigabe "rejected" und würden CI fälschlich rot färben)."""
    ci = [
        e for e in evaluations
        if str(((e.get("configuration") or {}).get("type") or {}).get("displayName", ""))
        .lower() in _CI_POLICY_TYPES
    ]
    if not ci:
        return "none"
    states = {str(e.get("status") or "").lower() for e in ci}
    if states & {"rejected", "broken"}:
        return "fail"
    if states & {"running", "queued"}:
        return "pending"
    if states <= {"approved", "notapplicable"}:
        return "pass"
    return "unknown"


def _strip_ref(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


class AzureReposCodeHost(_AzBase):
    """Azure-Repos-PRs (+ Pipelines-Status) als forge-Code-Host."""

    provider = "azure_devops"

    def __init__(self, *, config: AzureDevOpsConfig, repo_root: Path,
                 run_subprocess: SubprocessRunner = subprocess.run,
                 az_bin: str = "az") -> None:
        super().__init__(config=config, run_subprocess=run_subprocess, az_bin=az_bin)
        self.repo_root = repo_root

    # --- git ------------------------------------------------------------------

    def _git(self, args: list[str], *, what: str) -> subprocess.CompletedProcess:
        result = self._run(["git", *args], cwd=str(self.repo_root), **_KW)
        if result.returncode != 0:
            raise AzureReposError(f"{what} failed: {(result.stderr or '').strip()}")
        return result

    def push_branch(self, *, branch: str, remote: str = "origin",
                    target: str | None = None) -> None:
        argv = git_push_argv(branch=branch, remote=remote, target=target)
        self._git(argv[1:], what=" ".join(argv))

    # --- PRs -------------------------------------------------------------------

    def open_change(self, *, branch: str, title: str, body: str, base: str = "main",
                    labels: list[str] | None = None, draft: bool = False,
                    push: bool = True) -> PRCreationResult:
        if push:
            self.push_branch(branch=branch)
        description = body
        if len(description) > _PR_DESCRIPTION_LIMIT:
            description = description[: _PR_DESCRIPTION_LIMIT - 20] + "\n\n_(truncated)_"
        args = [
            "repos", "pr", "create",
            "--repository", self.config.repo_name,
            "--source-branch", branch,
            "--target-branch", base,
            "--title", title,
            "--description", description,
            *self._org(), *self._proj(),
        ]
        if draft:
            args += ["--draft", "true"]
        if labels:
            args += ["--labels", *labels]
        data = self._az_json(args, what="az repos pr create", error=AzureReposError)
        number = int(data["pullRequestId"])
        return PRCreationResult(pr_number=number, url=self._pr_url(number), branch=branch)

    def queue_auto_merge(self, *, pr_number: int, method: MergeMethod = "squash",
                         delete_branch: bool = True) -> None:
        self._az(
            ["repos", "pr", "update", "--id", str(pr_number), "--auto-complete", "true",
             "--squash", "true" if method == "squash" else "false",
             "--delete-source-branch", "true" if delete_branch else "false",
             *self._org(), "-o", "none"],
            what=f"az repos pr update {pr_number} --auto-complete",
            error=AzureReposError,
        )

    def _pr(self, pr_number: int) -> dict:
        return self._az_json(
            ["repos", "pr", "show", "--id", str(pr_number), *self._org()],
            what=f"az repos pr show {pr_number}",
            error=AzureReposError,
        )

    def _policies(self, pr_number: int) -> list[dict]:
        try:
            return self._az_json(
                ["repos", "pr", "policy", "list", "--id", str(pr_number), *self._org()],
                what=f"az repos pr policy list {pr_number}",
                error=AzureReposError,
            ) or []
        except AzureReposError:
            return []

    def fetch_metadata(self, pr_number: int) -> PRMetadata:
        data = self._pr(pr_number)
        return PRMetadata(
            number=int(data.get("pullRequestId", pr_number)),
            title=str(data.get("title") or ""),
            body=str(data.get("description") or ""),
            state=_PR_STATE.get(str(data.get("status") or "").lower(), "UNKNOWN"),
            base_branch=_strip_ref(str(data.get("targetRefName") or "main")),
            head_branch=_strip_ref(str(data.get("sourceRefName") or "")),
            ci_status=summarize_policies(self._policies(pr_number)),
            mergeable=_MERGEABLE.get(str(data.get("mergeStatus") or "").lower(), "UNKNOWN"),
        )

    def _fetch(self, branch: str) -> str:
        ref = f"refs/remotes/origin/{branch}"
        self._git(["fetch", "origin", f"+refs/heads/{branch}:{ref}"],
                  what=f"git fetch origin {branch}")
        return ref

    def fetch_diff(self, pr_number: int) -> str:
        meta = self.fetch_metadata(pr_number)
        head = self._fetch(meta.head_branch)
        base = self._fetch(meta.base_branch)
        return self._git(["diff", f"{base}...{head}"], what="git diff").stdout

    def head_committed_at(self, pr_number: int) -> datetime | None:
        try:
            meta = self.fetch_metadata(pr_number)
            head = self._fetch(meta.head_branch)
            raw = self._git(["log", "-1", "--format=%cI", head], what="git log").stdout.strip()
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (CodeHostError, ValueError, OSError):
            return None
        return dt.astimezone(UTC) if dt.tzinfo else dt.replace(tzinfo=UTC)

    def post_review(self, *, pr_number: int, approve: bool, body: str) -> None:
        self._az(
            ["repos", "pr", "set-vote", "--id", str(pr_number),
             "--vote", "approve" if approve else "wait-for-author", *self._org(), "-o", "none"],
            what=f"az repos pr set-vote {pr_number}",
            error=AzureReposError,
        )
        thread = {"comments": [{"parentCommentId": 0, "content": body, "commentType": 1}],
                  "status": 1}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as fh:
            json.dump(thread, fh)
            path = fh.name
        try:
            self._az(
                ["devops", "invoke", "--area", "git", "--resource", "pullRequestThreads",
                 "--route-parameters", f"project={self.config.project}",
                 f"repositoryId={self.config.repo_name}", f"pullRequestId={pr_number}",
                 "--http-method", "POST", "--in-file", path, "--api-version", "7.0",
                 *self._org(), "-o", "none"],
                what=f"az devops invoke pullRequestThreads {pr_number}",
                error=AzureReposError,
            )
        finally:
            Path(path).unlink(missing_ok=True)

    def merge(self, *, pr_number: int, method: MergeMethod = "squash",
              delete_branch: bool = True) -> MergeResult:
        if self.fetch_metadata(pr_number).state != "OPEN":
            raise AzureReposError(f"PR #{pr_number} is not active")
        self._az(
            ["repos", "pr", "update", "--id", str(pr_number), "--status", "completed",
             "--squash", "true" if method == "squash" else "false",
             "--delete-source-branch", "true" if delete_branch else "false",
             *self._org(), "-o", "none"],
            what=f"az repos pr update {pr_number} --status completed",
            error=AzureReposError,
        )
        return MergeResult(merged=True, merger="azure-devops", method=method)

    def create_release(self, *, tag: str, title: str, notes: str | None = None,
                       target: str | None = None) -> str:
        url = (f"{self.config.org_url}/{self.config.project}/_git/{self.config.repo_name}"
               f"?version=GT{tag}")
        remote = self._git(["ls-remote", "--tags", "origin", f"refs/tags/{tag}"],
                           what="git ls-remote").stdout.strip()
        if remote:
            return url
        message = f"{title}\n\n{notes}" if notes else title
        ref = "HEAD"
        if target:
            ref = self._fetch(target)
        self._git(["tag", "-a", tag, ref, "-m", message], what=f"git tag {tag}")
        self._git(["push", "origin", f"refs/tags/{tag}"], what=f"git push tag {tag}")
        return url

    # --- CI --------------------------------------------------------------------

    def ci_failure_summary(self, pr_number: int, *, max_chars: int = 8000) -> str:
        failed = [
            e for e in self._policies(pr_number)
            if str(e.get("status") or "").lower() in {"rejected", "broken"}
            and summarize_policies([e]) == "fail"
        ]
        if not failed:
            return ""
        lines = ["Failing checks:"]
        for e in failed:
            cfg = e.get("configuration") or {}
            name = ((cfg.get("settings") or {}).get("displayName")
                    or (cfg.get("type") or {}).get("displayName") or "policy")
            build = (e.get("context") or {}).get("buildId")
            link = (f" {self.config.org_url}/{self.config.project}/_build/results?buildId={build}"
                    if build else "")
            lines.append(f"- {name}{link}")
        return "\n".join(lines)[-max_chars:]

    def ref_ci_status(self, ref: str) -> str:
        try:
            runs = self._az_json(
                ["pipelines", "runs", "list", "--branch", f"refs/heads/{ref}", "--top", "1",
                 *self._org(), *self._proj()],
                what="az pipelines runs list",
                error=AzureReposError,
            ) or []
        except AzureReposError:
            return "unknown"
        if not runs:
            return "none"
        run = runs[0]
        if str(run.get("status") or "").lower() != "completed":
            return "pending"
        result = str(run.get("result") or "").lower()
        if result == "succeeded":
            return "pass"
        if result in {"failed", "canceled", "partiallysucceeded"}:
            return "fail"
        return "unknown"

    def list_open_changes(self) -> list[OpenChange]:
        data = self._az_json(
            ["repos", "pr", "list", "--repository", self.config.repo_name,
             "--status", "active", *self._org(), *self._proj()],
            what="az repos pr list",
            error=AzureReposError,
        ) or []
        return sorted(
            (
                OpenChange(
                    number=int(d["pullRequestId"]),
                    head_branch=_strip_ref(str(d.get("sourceRefName") or "")),
                    base_branch=_strip_ref(str(d.get("targetRefName") or "")),
                )
                for d in data
            ),
            key=lambda c: c.number,
        )

    def _pr_url(self, number: int) -> str:
        return (f"{self.config.org_url}/{self.config.project}/_git/"
                f"{self.config.repo_name}/pullrequest/{number}")


__all__ = [
    "AzureBoardsTracker",
    "AzureDevOpsError",
    "AzureReposCodeHost",
    "AzureReposError",
    "html_to_text",
    "summarize_policies",
    "text_to_html",
]
