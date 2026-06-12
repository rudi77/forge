"""ConductorTickCompleted payload.

Conductor-Design (Loop 2): der Heartbeat führt periodisch einen Tick aus
— Board-Pass + fällige schedule-Trigger. Jeder Tick ist ein Event
(Mantra 2: ohne Events keine Lernkurve), damit ``forge analyze`` und der
Replay den Fabrik-Durchsatz über die Zeit rekonstruieren können.

Das Event steht ÜBER den Runs: sein ``run_id`` ist die Conductor-Session-
ULID (der Heartbeat-Prozess), nicht eine Loop-Run-ID. Die dispatchten
Runs tragen ihre eigenen ``run_id``s und verweisen nicht zurück — die
Verknüpfung läuft zeitlich über ``ts`` (Tick-Fenster).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload


class ConductorTickCompletedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tick_index: int = Field(ge=0)
    """0-basierter Index des Ticks innerhalb der Heartbeat-Session."""

    dispatched: int = Field(default=0, ge=0)
    """Wie viele Work-Items in diesem Tick als Run dispatcht wurden."""

    scheduled: int = Field(default=0, ge=0)
    """Wie viele schedule-Trigger (cron) in diesem Tick fällig waren und
    feuerten."""

    blocked: int = Field(default=0, ge=0)
    """Work-Items, die nicht voran konnten (Dependencies/Cycle/Fehler).
    In Phase B (reiner Heartbeat) immer 0 — wird in Phase C (Conductor mit
    State-Machine) befüllt."""

    skipped: int = Field(default=0, ge=0)
    """Work-Items, die übersprungen wurden (z.B. Triage-Filter)."""

    bailed: bool = False
    """True, wenn der Board-Pass abbrach (Cost-Cap/Guardrail/Fehler)."""

    scheduled_resume_count: int = Field(default=0, ge=0)
    """Wie viele vom Usage-/Session-Limit unterbrochene Runs in diesem Tick
    fällig waren und per ``forge run --resume`` wieder angestoßen wurden."""


# 1.1 (additiv): scheduled_resume_count ergänzt — alte 1.0-Events lesen weiter.
register_payload(
    EventKind.CONDUCTOR_TICK_COMPLETED, ConductorTickCompletedPayload, "1.1"
)
