"""GitHub-Adapter.

Vier Komponenten:

- `pr.py`            — Wrapper um `gh pr create` + `gh pr merge --auto`
- `board.py`         — GitHub-Project-Board als aktive Trigger-Quelle (v0.4)
- `webhook.py`       — minimaler Webhook-Listener für PRMerged/PRReverted
- `templates/`       — YAML-Templates für GitHub Actions (Issue, PR, CI, Schedule)
"""

from forge_adapters.github.board import (
    BoardError,
    ReadyIssue,
    list_ready_items,
    wrap_issue_body,
)
from forge_adapters.github.pr import (
    GitHubError,
    PRCreationResult,
    create_pr_for_run,
    push_branch,
    queue_auto_merge,
    render_pr_body,
)
from forge_adapters.github.webhook import (
    WebhookEvent,
    record_pr_merged,
    record_pr_reverted,
)

__all__ = [
    "BoardError",
    "GitHubError",
    "PRCreationResult",
    "ReadyIssue",
    "WebhookEvent",
    "create_pr_for_run",
    "list_ready_items",
    "push_branch",
    "queue_auto_merge",
    "record_pr_merged",
    "record_pr_reverted",
    "render_pr_body",
    "wrap_issue_body",
]
