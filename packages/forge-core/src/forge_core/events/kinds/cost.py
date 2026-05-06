"""CostCapHit payload.

Vier Ebenen: per-generation, per-run, per-project-per-day, per-project-per-month.
Verhalten pro Ebene siehe Spec Teil 5.3.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

from forge_core.events.base import EventKind, register_payload

CostCapLevel = Literal["generation", "run", "project_day", "project_month"]


class CostCapHitPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: CostCapLevel
    cap_usd: Decimal
    actual_usd: Decimal


register_payload(EventKind.COST_CAP_HIT, CostCapHitPayload, "1.0")
