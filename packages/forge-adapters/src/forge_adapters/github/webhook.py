"""Minimaler Webhook-Listener für PRMerged / PRReverted.

In v1 läuft kein eigener HTTP-Server — die GitHub Action Templates rufen
diese Funktionen direkt auf, sobald ein PR-Merge oder Revert erkannt wird.
Die Action lädt sich `forge` als pip-Package und ruft via Subprocess oder
direktem Python-Import die hier definierten Funktionen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from forge_core.events import (
    EventKind,
    PRMergedPayload,
    PRRevertedPayload,
    build_event,
)
from forge_core.store import EventStore

WebhookKind = Literal["pull_request_merged", "pull_request_reverted"]


@dataclass
class WebhookEvent:
    """Minimal-Repräsentation eines GitHub-Webhook-Events.

    Wird vom Adapter aus dem rohen Webhook-JSON gefüllt — die Felder sind
    das Subset, das forge tatsächlich braucht.
    """

    kind: WebhookKind
    pr_number: int
    merger: str | None = None
    merged_at: datetime | None = None
    created_at: datetime | None = None
    base_branch: str = "main"
    head_branch: str = ""


def record_pr_merged(
    *,
    pr_number: int,
    merger: str,
    time_to_merge_s: int,
    run_id: str,
    project: str,
    project_fingerprint: str,
    factory_version: str,
    spec_version: str,
    store: EventStore,
) -> None:
    """Schreibt ein `PRMerged`-Event in den Store.

    Wird in v1 von der `forge-pr-merged.yml`-Action aufgerufen, wenn GitHub
    `pull_request.merged=true` meldet.
    """
    evt = build_event(
        kind=EventKind.PR_MERGED,
        run_id=run_id,
        project=project,
        project_fingerprint=project_fingerprint,
        factory_version=factory_version,
        spec_version=spec_version,
        payload=PRMergedPayload(
            pr_number=pr_number,
            merger=merger,
            time_to_merge_s=time_to_merge_s,
        ),
    )
    store.append(evt)


def record_pr_reverted(
    *,
    pr_number: int,
    revert_reason: Literal[
        "explicit_revert_commit", "branch_hard_reset", "manual_signal"
    ],
    original_pr: int,
    run_id: str,
    project: str,
    project_fingerprint: str,
    factory_version: str,
    spec_version: str,
    store: EventStore,
) -> None:
    """Schreibt ein `PRReverted`-Event in den Store."""
    evt = build_event(
        kind=EventKind.PR_REVERTED,
        run_id=run_id,
        project=project,
        project_fingerprint=project_fingerprint,
        factory_version=factory_version,
        spec_version=spec_version,
        payload=PRRevertedPayload(
            pr_number=pr_number,
            revert_reason=revert_reason,
            original_pr=original_pr,
        ),
    )
    store.append(evt)


def find_run_id_for_pr(*, store: EventStore, pr_number: int) -> str | None:
    """Sucht den Run, der diesen PR erzeugt hat.

    Webhooks tragen die PR-Nummer; der ursprüngliche `PRCreated`-Event
    verknüpft sie mit dem Run.
    """
    rows = store.query(
        """
        SELECT run_id
        FROM events
        WHERE kind = 'PRCreated'
          AND CAST(json_extract(payload, '$.pr_number') AS BIGINT) = ?
        ORDER BY ts DESC
        LIMIT 1
        """,
        [pr_number],
    )
    return rows[0]["run_id"] if rows else None
