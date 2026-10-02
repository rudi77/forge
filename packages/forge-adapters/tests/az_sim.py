"""Minimaler ``az``-(azure-devops)/git-Simulator für die Vertrags-Testsuite.

Beantwortet genau die argv-Formen, die ``AzureBoardsTracker``/
``AzureReposCodeHost`` absetzen, inkl. einer kleinen WIQL-Auswertung (Tags-
CONTAINS mit AND/OR, State-Filter, Description-CONTAINS-WORDS). Unbekannte
Kommandos → Exit 1.
"""

from __future__ import annotations

import html
import json
import re
import subprocess
from dataclasses import dataclass, field


def _ok(out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0, out, "")


def _fail(msg: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 1, "", msg)


def _opt(argv: list[str], flag: str) -> str | None:
    return argv[argv.index(flag) + 1] if flag in argv else None


def _unq(s: str) -> str:
    return s.replace("''", "'")


@dataclass
class AzSim:
    items: dict[int, dict] = field(default_factory=dict)
    prs: dict[int, dict] = field(default_factory=dict)
    tags_remote: set[str] = field(default_factory=set)
    pushed: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    threads: list[tuple[int, str]] = field(default_factory=list)

    def add_item(self, number, title, body="", labels=(), open_=True, wit="Task"):
        self.items[number] = {
            "id": number,
            "fields": {
                "System.Id": number,
                "System.Title": title,
                "System.Description": html.escape(body).replace("\n", "<br>"),
                "System.Tags": "; ".join(labels),
                "System.State": "Active" if open_ else "Closed",
                "System.WorkItemType": wit,
            },
            "comments": [],
        }

    def __call__(self, argv, **_kw) -> subprocess.CompletedProcess:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return self._git(argv[1:])
        if argv[0] != "az":
            return _fail("unexpected binary")
        head = argv[1:4]
        if head[:2] == ["boards", "query"]:
            return self._query(_opt(argv, "--wiql") or "")
        if head == ["boards", "work-item", "show"]:
            n = int(_opt(argv, "--id"))
            return _ok(json.dumps(self.items[n])) if n in self.items else _fail("not found")
        if head == ["boards", "work-item", "update"]:
            return self._update(argv)
        if head == ["boards", "work-item", "create"]:
            n = max(self.items, default=0) + 1
            tags = (_opt(argv, "--fields") or "").removeprefix("System.Tags=")
            self.add_item(n, _opt(argv, "--title"), "", [t.strip() for t in tags.split(";")
                                                         if t.strip()],
                          wit=_opt(argv, "--type"))
            self.items[n]["fields"]["System.Description"] = _opt(argv, "--description")
            return _ok(json.dumps(self.items[n]))
        if head == ["boards", "work-item", "relation"]:
            return _ok("{}")
        if head == ["repos", "pr", "create"]:
            n = 500 + len(self.prs) + 1
            self.prs[n] = {
                "pullRequestId": n,
                "title": _opt(argv, "--title"),
                "description": _opt(argv, "--description"),
                "status": "active",
                "sourceRefName": "refs/heads/" + _opt(argv, "--source-branch"),
                "targetRefName": "refs/heads/" + _opt(argv, "--target-branch"),
                "mergeStatus": "succeeded",
                "votes": [],
            }
            return _ok(json.dumps(self.prs[n]))
        if head == ["repos", "pr", "show"]:
            n = int(_opt(argv, "--id"))
            return _ok(json.dumps(self.prs[n])) if n in self.prs else _fail("no pr")
        if head == ["repos", "pr", "policy"]:
            n = int(_opt(argv, "--id"))
            if n not in self.prs:
                return _fail("no pr")
            return _ok(json.dumps([{"status": "approved",
                                    "configuration": {"type": {"displayName": "Build"}}}]))
        if head == ["repos", "pr", "update"]:
            n = int(_opt(argv, "--id"))
            if n not in self.prs or self.prs[n]["status"] != "active":
                return _fail("not active")
            if _opt(argv, "--status") == "completed":
                self.prs[n]["status"] = "completed"
            return _ok()
        if head == ["repos", "pr", "set-vote"]:
            n = int(_opt(argv, "--id"))
            if n not in self.prs:
                return _fail("no pr")
            self.prs[n]["votes"].append(_opt(argv, "--vote"))
            return _ok()
        if head == ["repos", "pr", "list"]:
            return _ok(json.dumps([p for p in self.prs.values() if p["status"] == "active"]))
        if head[:2] == ["devops", "invoke"]:
            route = " ".join(argv)
            m = re.search(r"pullRequestId=(\d+)", route)
            self.threads.append((int(m.group(1)) if m else 0, route))
            return _ok()
        if head[:3] == ["pipelines", "runs", "list"]:
            return _ok("[]")
        return _fail(f"unsupported az command {head}")

    # --- boards -------------------------------------------------------------

    def _query(self, wiql: str) -> subprocess.CompletedProcess:
        where = wiql.split(" WHERE ", 1)[1].split(" ORDER BY ")[0]
        if "[System.Id] < 0" in where:
            return _ok("[]")
        tags = [_unq(t) for t in re.findall(r"\[System\.Tags\] CONTAINS '((?:[^']|'')*)'", where)]
        any_mode = " OR " in where
        not_in = re.search(r"\[System\.State\] NOT IN \(([^)]*)\)", where)
        in_ = re.search(r"\[System\.State\] IN \(([^)]*)\)", where)
        eq = re.search(r"\[System\.State\] = '((?:[^']|'')*)'", where)
        words = re.search(r"CONTAINS WORDS '((?:[^']|'')*)'", where)

        def states(m):
            return {_unq(s.strip().strip("'")) for s in m.group(1).split(",")}

        out = []
        for it in self.items.values():
            f = it["fields"]
            item_tags = [t.strip() for t in f["System.Tags"].split(";") if t.strip()]
            if tags:
                hits = [t in item_tags for t in tags]
                if not (any(hits) if any_mode else all(hits)):
                    continue
            if not_in and f["System.State"] in states(not_in):
                continue
            if in_ and f["System.State"] not in states(in_):
                continue
            if eq and f["System.State"] != _unq(eq.group(1)):
                continue
            if words:
                text = html.unescape(re.sub(r"<[^>]+>", " ", f["System.Description"]))
                if not all(w in text for w in _unq(words.group(1)).split()):
                    continue
            out.append({"id": it["id"], "fields": f})
        return _ok(json.dumps(out))

    def _update(self, argv) -> subprocess.CompletedProcess:
        n = int(_opt(argv, "--id"))
        if n not in self.items:
            return _fail("not found")
        f = self.items[n]["fields"]
        fields = _opt(argv, "--fields")
        if fields and fields.startswith("System.Tags="):
            f["System.Tags"] = fields.removeprefix("System.Tags=")
        if _opt(argv, "--state"):
            f["System.State"] = _opt(argv, "--state")
        if _opt(argv, "--discussion"):
            self.items[n]["comments"].append(_opt(argv, "--discussion"))
        return _ok()

    # --- git ----------------------------------------------------------------

    def _git(self, args: list[str]) -> subprocess.CompletedProcess:
        if args[0] == "push":
            self.pushed.append(args[-1])
            if args[-1].startswith("refs/tags/"):
                self.tags_remote.add(args[-1].removeprefix("refs/tags/"))
            return _ok()
        if args[0] == "ls-remote":
            tag = args[-1].removeprefix("refs/tags/")
            return _ok(f"abc\trefs/tags/{tag}\n" if tag in self.tags_remote else "")
        if args[0] in {"fetch", "tag"}:
            return _ok()
        if args[0] == "diff":
            return _ok("diff --git a/x b/x\n")
        if args[0] == "log":
            return _ok("2026-01-01T10:00:00+00:00\n")
        return _fail(f"unsupported git {args}")
