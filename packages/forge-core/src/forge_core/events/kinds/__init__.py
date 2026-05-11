"""Sub-Schemas pro EventKind.

Jeder Kind hat seine eigene SemVer (`payload_schema_version`). Das `EvalFinished`-
Schema kann sich unabhängig vom `ProposalReceived`-Schema weiterentwickeln —
globales Schema-Versioning skaliert nicht (Spec Teil 4.1).

Beim Import dieses Pakets registrieren sich alle Sub-Schemas im Registry der
Base-Modul.
"""

from forge_core.events.kinds.cost import CostCapHitPayload
from forge_core.events.kinds.decision import DecisionMadePayload
from forge_core.events.kinds.eval_ import EvalFinishedPayload, EvalStartedPayload
from forge_core.events.kinds.generation import (
    GenerationFinishedPayload,
    GenerationStartedPayload,
)
from forge_core.events.kinds.guardrail import GuardrailViolationPayload
from forge_core.events.kinds.mutation import MutationAppliedPayload
from forge_core.events.kinds.plan import PlanProposedPayload, RiskLevel
from forge_core.events.kinds.pr import (
    PRCreatedPayload,
    PRMergedPayload,
    PRRevertedPayload,
)
from forge_core.events.kinds.preflight import PreflightFailedPayload
from forge_core.events.kinds.proposal import (
    ProposalReceivedPayload,
    ProposalRequestedPayload,
)
from forge_core.events.kinds.run import RunFinishedPayload, RunStartedPayload
from forge_core.events.kinds.triage import IssueTriagedPayload, TriageDecision

__all__ = [
    "CostCapHitPayload",
    "DecisionMadePayload",
    "EvalFinishedPayload",
    "EvalStartedPayload",
    "GenerationFinishedPayload",
    "GenerationStartedPayload",
    "GuardrailViolationPayload",
    "IssueTriagedPayload",
    "MutationAppliedPayload",
    "PRCreatedPayload",
    "PRMergedPayload",
    "PRRevertedPayload",
    "PlanProposedPayload",
    "PreflightFailedPayload",
    "ProposalReceivedPayload",
    "ProposalRequestedPayload",
    "RiskLevel",
    "RunFinishedPayload",
    "RunStartedPayload",
    "TriageDecision",
]
