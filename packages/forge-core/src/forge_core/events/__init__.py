"""Event-Schema für forge.

Events sind die einzige Wahrheit, aus der spätere Auswertung Empfehlungen ableitet.
Das Schema ist die einzige Strukturentscheidung, die später nicht mehr korrigierbar ist
(siehe Spec Teil 4).

Verwendung:

    from forge_core.events import Event, EventKind, RunStartedPayload
    from forge_core.events import build_event

    evt = build_event(
        kind=EventKind.RUN_STARTED,
        run_id="01HZX...",
        project="pinta",
        project_fingerprint="sha256:7a3c...",
        factory_version="git:e8f4a92",
        spec_version="1.0",
        payload=RunStartedPayload(
            trigger="issue_label",
            strategy="sequential",
            ...
        ),
    )
"""

from forge_core.events.base import (
    Event,
    EventKind,
    EventValidationError,
    build_event,
)
from forge_core.events.kinds import (
    ConductorTickCompletedPayload,
    CostCapHitPayload,
    DecisionMadePayload,
    EvalFinishedPayload,
    EvalStartedPayload,
    GenerationFinishedPayload,
    GenerationStartedPayload,
    GuardrailViolationPayload,
    IssueTriagedPayload,
    MutationAppliedPayload,
    PlanProposedPayload,
    PlanSubtask,
    PRCreatedPayload,
    PreflightFailedPayload,
    PRMergedPayload,
    ProposalReceivedPayload,
    ProposalRequestedPayload,
    PRRevertedPayload,
    PRReviewedPayload,
    RequirementsRefinedPayload,
    RiskLevel,
    RunFinishedPayload,
    RunResumeScheduledPayload,
    RunStartedPayload,
    SubtaskStatus,
    TriageDecision,
    WorkItemBlockedPayload,
    WorkItemStageChangedPayload,
)

__all__ = [
    "ConductorTickCompletedPayload",
    "CostCapHitPayload",
    "DecisionMadePayload",
    "EvalFinishedPayload",
    "EvalStartedPayload",
    "Event",
    "EventKind",
    "EventValidationError",
    "GenerationFinishedPayload",
    "GenerationStartedPayload",
    "GuardrailViolationPayload",
    "IssueTriagedPayload",
    "MutationAppliedPayload",
    "PRCreatedPayload",
    "PRMergedPayload",
    "PRRevertedPayload",
    "PRReviewedPayload",
    "PlanProposedPayload",
    "PlanSubtask",
    "PreflightFailedPayload",
    "ProposalReceivedPayload",
    "ProposalRequestedPayload",
    "RequirementsRefinedPayload",
    "RiskLevel",
    "RunFinishedPayload",
    "RunResumeScheduledPayload",
    "RunStartedPayload",
    "SubtaskStatus",
    "TriageDecision",
    "WorkItemBlockedPayload",
    "WorkItemStageChangedPayload",
    "build_event",
]
