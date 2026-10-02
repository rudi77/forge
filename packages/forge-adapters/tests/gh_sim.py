"""Minimaler ``gh``/``git push``-Simulator für Adapter-Tests.

Hält Issues, Labels, PRs und Releases im Speicher und beantwortet genau die
argv-Formen, die ``GitHubTracker``/``GitHubCodeHost`` absetzen. Damit läuft die
Vertrags-Testsuite gegen den echten GitHub-Adapter (argv-Bau + JSON-Parsing),
ohne Netz. Unbekannte Kommandos → Exit 1 (der Test sieht den Fehler).
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _fail(msg: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", msg)


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _opts(argv: list[str], flag: str) -> list[str]:
    return [argv[i + 1] for i, a in enumerate(argv) if a == flag and i + 1 < len(argv)]


@dataclass
class GhSim:
    issues: dict[int, dict] = field(default_factory=dict)
    labels: set[str] = field(default_factory=set)
    prs: dict[int, dict] = field(default_factory=dict)
    releases: dict[str, str] = field(default_factory=dict)
    pushed: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)

    def __call__(self, argv, **_kw) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv[:2] == ["git", "push"]:
            self.pushed.append(argv[-1])
            return _ok()
        if argv[0] != "gh":
            return _fail(f"unexpected binary {argv[0]}")
        cmd = argv[1:3]
        handler = {
            ("issue", "list"): self._issue_list,
            ("issue", "view"): self._issue_view,
            ("issue", "edit"): self._issue_edit,
            ("issue", "comment"): self._issue_comment,
            ("issue", "close"): self._issue_close,
            ("issue", "create"): self._issue_create,
            ("label", "list"): self._label_list,
            ("label", "create"): self._label_create,
            ("pr", "create"): self._pr_create,
            ("pr", "view"): self._pr_view,
            ("pr", "diff"): self._pr_diff,
            ("pr", "review"): self._pr_review,
            ("pr", "merge"): self._pr_merge,
            ("pr", "list"): self._pr_list,
            ("release", "view"): self._release_view,
            ("release", "create"): self._release_create,
        }.get(tuple(cmd))
        if argv[1] == "api":
            return self._api(argv)
        if handler is None:
            return _fail(f"unsupported gh command {cmd}")
        return handler(argv)

    # --- issues ---------------------------------------------------------

    def add_issue(self, number: int, title: str, body: str = "", labels=(), open_=True):
        self.issues[number] = {
            "number": number,
            "title": title,
            "body": body,
            "labels": [{"name": lbl} for lbl in labels],
            "url": f"https://github.com/o/r/issues/{number}",
            "state": "OPEN" if open_ else "CLOSED",
            "comments": [],
        }
        self.labels.update(labels)

    def _issue_list(self, argv):
        state = (_opt(argv, "--state") or "open").upper()
        search = _opt(argv, "--search")
        out = []
        for it in self.issues.values():
            if state != "ALL" and it["state"] != state:
                continue
            if search and search.split('"')[1] not in it["body"]:
                continue
            out.append({k: it[k] for k in ("number", "title", "body", "labels", "url")})
        return _ok(json.dumps(out))

    def _issue_view(self, argv):
        n = int(argv[3])
        if n not in self.issues:
            return _fail(f"issue {n} not found")
        return _ok(json.dumps(self.issues[n]))

    def _issue_edit(self, argv):
        n = int(argv[3])
        it = self.issues.get(n)
        if it is None:
            return _fail("not found")
        names = [lbl["name"] for lbl in it["labels"]]
        for rm in _opts(argv, "--remove-label"):
            names = [x for x in names if x != rm]
        for add in _opts(argv, "--add-label"):
            if add not in names:
                names.append(add)
        it["labels"] = [{"name": x} for x in names]
        self.labels.update(names)
        return _ok()

    def _issue_comment(self, argv):
        n = int(argv[3])
        if n not in self.issues:
            return _fail("not found")
        self.issues[n]["comments"].append(_opt(argv, "--body"))
        return _ok()

    def _issue_close(self, argv):
        n = int(argv[3])
        if n not in self.issues:
            return _fail("not found")
        self.issues[n]["state"] = "CLOSED"
        return _ok()

    def _issue_create(self, argv):
        n = max(self.issues, default=0) + 1
        self.add_issue(n, _opt(argv, "--title") or "", _opt(argv, "--body") or "",
                       _opts(argv, "--label"))
        return _ok(f"https://github.com/o/r/issues/{n}\n")

    def _label_list(self, argv):
        return _ok(json.dumps([{"name": n} for n in sorted(self.labels)]))

    def _label_create(self, argv):
        self.labels.add(argv[3])
        return _ok()

    # --- PRs --------------------------------------------------------------

    def _pr_create(self, argv):
        n = 100 + len(self.prs) + 1
        self.prs[n] = {
            "number": n,
            "title": _opt(argv, "--title"),
            "body": _opt(argv, "--body"),
            "state": "OPEN",
            "baseRefName": _opt(argv, "--base"),
            "headRefName": _opt(argv, "--head"),
            "mergeable": "MERGEABLE",
            "statusCheckRollup": [{"status": "COMPLETED", "conclusion": "SUCCESS"}],
            "commits": [{"committedDate": "2026-01-01T10:00:00Z"}],
            "reviews": [],
            "diff": "diff --git a/x b/x\n",
        }
        return _ok(f"https://github.com/o/r/pull/{n}\n")

    def _pr_view(self, argv):
        n = int(argv[3])
        if n not in self.prs:
            return _fail("no pr")
        return _ok(json.dumps(self.prs[n]))

    def _pr_diff(self, argv):
        n = int(argv[3])
        return _ok(self.prs[n]["diff"]) if n in self.prs else _fail("no pr")

    def _pr_review(self, argv):
        n = int(argv[3])
        if n not in self.prs:
            return _fail("no pr")
        self.prs[n]["reviews"].append(("--approve" in argv, _opt(argv, "--body")))
        return _ok()

    def _pr_merge(self, argv):
        n = int(argv[3])
        if n not in self.prs or self.prs[n]["state"] != "OPEN":
            return _fail("not mergeable")
        if "--auto" not in argv:
            self.prs[n]["state"] = "MERGED"
        return _ok()

    def _pr_list(self, argv):
        out = [
            {k: p[k] for k in ("number", "headRefName", "baseRefName")}
            for p in self.prs.values()
            if p["state"] == "OPEN"
        ]
        return _ok(json.dumps(out))

    # --- releases + api ---------------------------------------------------

    def _release_view(self, argv):
        tag = argv[3]
        if tag not in self.releases:
            return _fail("release not found")
        return _ok(json.dumps({"url": self.releases[tag]}))

    def _release_create(self, argv):
        tag = argv[3]
        self.releases[tag] = f"https://github.com/o/r/releases/tag/{tag}"
        return _ok(self.releases[tag] + "\n")

    def _api(self, argv):
        path = next((a for a in argv[2:] if not a.startswith("-") and "/" in a), "")
        if path == "user":
            return _ok("forge-bot\n")
        if path.endswith("/check-runs"):
            return _ok(json.dumps({"check_runs": [{"status": "completed", "conclusion": "success"}]}))
        if "graphql" in argv:
            return _ok("true\n")
        if "/sub_issues" in path:
            return _ok("{}")
        if "/issues/" in path:
            return _ok("123456\n")
        if "nameWithOwner" in argv:
            return _ok("o/r\n")
        return _ok("{}")
