"""Azure-DevOps-Adapter (Boards als WorkTracker, Repos/Pipelines als CodeHost).

- `adapter.py`   — `AzureBoardsTracker` + `AzureReposCodeHost` über die `az`-CLI
- `templates/`   — Azure-Pipelines-YAML (Conductor-Heartbeat, PR-Review)
"""

from forge_adapters.azure.adapter import (
    AzureBoardsTracker,
    AzureDevOpsError,
    AzureReposCodeHost,
    AzureReposError,
    html_to_text,
    summarize_policies,
    text_to_html,
)

__all__ = [
    "AzureBoardsTracker",
    "AzureDevOpsError",
    "AzureReposCodeHost",
    "AzureReposError",
    "html_to_text",
    "summarize_policies",
    "text_to_html",
]
