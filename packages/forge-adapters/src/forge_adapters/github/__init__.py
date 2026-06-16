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
    list_stage_items,
    set_issue_stage_label,
    wrap_issue_body,
)
from forge_adapters.github.pr import (
    GitHubError,
    MergeResult,
    PRCreationResult,
    PRMetadata,
    create_pr_for_run,
    create_release,
    fetch_pr_diff,
    fetch_pr_head_committed_at,
    fetch_pr_metadata,
    gh_current_login,
    merge_pr,
    post_pr_review,
    push_branch,
    queue_auto_merge,
    render_pr_body,
    summarize_ci,
)
from forge_adapters.github.webhook import (
    WebhookEvent,
    record_pr_merged,
    record_pr_reverted,
)

__all__ = [
    "BoardError",
    "GitHubError",
    "MergeResult",
    "PRCreationResult",
    "PRMetadata",
    "ReadyIssue",
    "WebhookEvent",
    "create_pr_for_run",
    "create_release",
    "fetch_pr_diff",
    "fetch_pr_head_committed_at",
    "fetch_pr_metadata",
    "gh_current_login",
    "list_ready_items",
    "list_stage_items",
    "merge_pr",
    "post_pr_review",
    "push_branch",
    "queue_auto_merge",
    "record_pr_merged",
    "record_pr_reverted",
    "render_pr_body",
    "set_issue_stage_label",
    "summarize_ci",
    "wrap_issue_body",
]
