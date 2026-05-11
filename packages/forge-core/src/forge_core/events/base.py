"""Base-Event-Modell und EventKind-Enum.

Aus der Spec Teil 4.1 — drei Punkte sind besonders wichtig:

1. **`payload_schema_version` pro Kind.** Jedes Event-Kind hat sein eigenes
   Pydantic-Sub-Schema mit eigener SemVer.
2. **Artefakte als CAS-References.** Prompts, Diffs, Eval-Outputs werden als
   `sha256` referenziert, nicht inline gespeichert.
3. **Tool-Versionen sind Teil des Replay-Kontrakts.** Bei jedem `EvalFinished`
   wird ein `tool_versions`-Artefakt mitgespeichert.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from ulid import ULID


class EventKind(StrEnum):
    """Alle in v1 produzierbaren Event-Kinds (Spec Teil 4.2)."""

    RUN_STARTED = "RunStarted"
    RUN_FINISHED = "RunFinished"
    GENERATION_STARTED = "GenerationStarted"
    GENERATION_FINISHED = "GenerationFinished"
    PLAN_PROPOSED = "PlanProposed"
    PROPOSAL_REQUESTED = "ProposalRequested"
    PROPOSAL_RECEIVED = "ProposalReceived"
    MUTATION_APPLIED = "MutationApplied"
    PREFLIGHT_FAILED = "PreflightFailed"
    EVAL_STARTED = "EvalStarted"
    EVAL_FINISHED = "EvalFinished"
    DECISION_MADE = "DecisionMade"
    PR_CREATED = "PRCreated"
    PR_MERGED = "PRMerged"
    PR_REVERTED = "PRReverted"
    COST_CAP_HIT = "CostCapHit"
    GUARDRAIL_VIOLATION = "GuardrailViolation"
    ISSUE_TRIAGED = "IssueTriaged"


class EventValidationError(ValueError):
    """Schema-Verletzung beim Event-Bau oder Laden."""


def _new_event_id() -> str:
    return str(ULID())


def _utc_now() -> datetime:
    return datetime.now(UTC)


class Event(BaseModel):
    """Typisiertes, immutables Event mit referenzierten Artefakten.

    Felder gemäß Spec Teil 4.1. Pydantic v2 frozen — Events sind nach Konstruktion
    nicht mehr veränderbar.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=_new_event_id)
    run_id: str
    generation_id: str | None = None
    parent_event_id: str | None = None

    project: str
    project_fingerprint: str
    factory_version: str
    spec_version: str

    ts: datetime = Field(default_factory=_utc_now)
    duration_ms: int | None = None

    kind: EventKind
    payload_schema_version: str
    payload: dict[str, Any]

    artifacts: dict[str, str] = Field(default_factory=dict)

    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal("0")
    model: str | None = None
    model_version: str | None = None

    success: bool | None = None
    error_class: str | None = None
    error_msg: str | None = None

    @field_validator("artifacts")
    @classmethod
    def _validate_artifact_hashes(cls, value: dict[str, str]) -> dict[str, str]:
        for name, sha in value.items():
            if not sha.startswith("sha256:"):
                raise EventValidationError(
                    f"artifact {name!r}: hash must be prefixed 'sha256:', got {sha!r}"
                )
            hex_part = sha[len("sha256:") :]
            if len(hex_part) != 64 or not all(c in "0123456789abcdef" for c in hex_part):
                raise EventValidationError(
                    f"artifact {name!r}: expected 64-char lowercase hex after sha256:, got {sha!r}"
                )
        return value

    @field_validator("ts")
    @classmethod
    def _ts_must_be_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise EventValidationError("ts must be timezone-aware (UTC)")
        if value.utcoffset() != timedelta(0):
            raise EventValidationError(f"ts must be UTC, got offset {value.utcoffset()}")
        return value


# Registry: EventKind -> (payload_model, current_schema_version)
# Wird in events/kinds/__init__.py via register_payload() befüllt.
_PAYLOAD_REGISTRY: dict[EventKind, tuple[type[BaseModel], str]] = {}


def register_payload(kind: EventKind, model: type[BaseModel], version: str) -> None:
    """Registriert ein Sub-Schema für einen EventKind.

    Wird beim Import der `events.kinds`-Module aufgerufen — pro Kind genau einmal.
    Doppelregistrierung ist ein Programmierfehler (Schema-Konflikt).
    """
    if kind in _PAYLOAD_REGISTRY:
        existing, existing_version = _PAYLOAD_REGISTRY[kind]
        raise EventValidationError(
            f"payload schema for {kind} already registered "
            f"(existing: {existing.__name__} v{existing_version})"
        )
    _PAYLOAD_REGISTRY[kind] = (model, version)


def get_payload_model(kind: EventKind) -> tuple[type[BaseModel], str]:
    """Liefert (Pydantic-Modell, schema_version) für einen Kind."""
    if kind not in _PAYLOAD_REGISTRY:
        raise EventValidationError(f"no payload schema registered for {kind}")
    return _PAYLOAD_REGISTRY[kind]


def build_event(
    *,
    kind: EventKind,
    run_id: str,
    project: str,
    project_fingerprint: str,
    factory_version: str,
    spec_version: str,
    payload: BaseModel | dict[str, Any],
    generation_id: str | None = None,
    parent_event_id: str | None = None,
    artifacts: dict[str, str] | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    cost_usd: Decimal | str | float = Decimal("0"),
    model: str | None = None,
    model_version: str | None = None,
    duration_ms: int | None = None,
    success: bool | None = None,
    error_class: str | None = None,
    error_msg: str | None = None,
    ts: datetime | None = None,
) -> Event:
    """Erzeugt ein validiertes Event.

    Validiert den Payload gegen das registrierte Sub-Schema des Kinds und stempelt
    die aktuelle `payload_schema_version` ein. Wenn `payload` ein Dict ist, wird es
    durchs Sub-Modell geschickt — das wirft bei Verletzung.
    """
    payload_model, schema_version = get_payload_model(kind)

    if isinstance(payload, BaseModel):
        if not isinstance(payload, payload_model):
            raise EventValidationError(
                f"payload type mismatch for {kind}: expected {payload_model.__name__}, "
                f"got {type(payload).__name__}"
            )
        payload_dict = payload.model_dump(mode="json")
    else:
        # Dict → durchs Modell validieren, danach als Dict zurück
        validated = payload_model.model_validate(payload)
        payload_dict = validated.model_dump(mode="json")

    return Event(
        run_id=run_id,
        generation_id=generation_id,
        parent_event_id=parent_event_id,
        project=project,
        project_fingerprint=project_fingerprint,
        factory_version=factory_version,
        spec_version=spec_version,
        ts=ts if ts is not None else _utc_now(),
        duration_ms=duration_ms,
        kind=kind,
        payload_schema_version=schema_version,
        payload=payload_dict,
        artifacts=artifacts or {},
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=Decimal(str(cost_usd)),
        model=model,
        model_version=model_version,
        success=success,
        error_class=error_class,
        error_msg=error_msg,
    )
