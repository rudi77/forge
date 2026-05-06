"""GitHub-Adapter.

Drei Komponenten gemäß todos.txt Schritt 4b:

- `pr.py`            — Wrapper um `gh pr create` mit strukturiertem Body
- `webhook.py`       — minimaler Webhook-Listener für PRMerged/PRReverted
- `templates/`       — YAML-Templates für GitHub Actions (Issue, PR, CI, Schedule)
"""

from forge_adapters.github.pr import (
    GitHubError,
    PRCreationResult,
    create_pr_for_run,
    push_branch,
    render_pr_body,
)
from forge_adapters.github.webhook import (
    WebhookEvent,
    record_pr_merged,
    record_pr_reverted,
)

__all__ = [
    "GitHubError",
    "PRCreationResult",
    "WebhookEvent",
    "create_pr_for_run",
    "push_branch",
    "record_pr_merged",
    "record_pr_reverted",
    "render_pr_body",
]
