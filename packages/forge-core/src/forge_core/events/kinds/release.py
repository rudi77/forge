"""ReleaseTagged payload (Pipeline-Ende hinten / Conductor-Stage release).

Anders als die anderen Stage-Outputs ist der Release **kein** LLM-Run, sondern
ein **deterministischer forge-Effekt** (wie der Merge): forge erzeugt in der
``release``-Stage nach dem Merge einen Tag + GitHub-Release via
``gh release create`` — opt-in über ``capabilities.create_release``. Daraus
leitet ``derive_signals`` ``release_done`` ab → der Conductor schreibt
``release→done`` fort. Korreliert über ``issue_number`` (es gibt keinen
``RunStarted`` für den deterministischen Effekt).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from forge_core.events.base import EventKind, register_payload


class ReleaseTaggedPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int = Field(gt=0)
    tag: str
    release_url: str | None = None
    changelog_blob: str | None = None
    """Optionaler CAS-Ref auf den Changelog-Text (v1: meist ``None`` —
    ``gh release create --generate-notes`` erzeugt die Notes server-seitig)."""

    version: str | None = None
    """1.1: SemVer des Releases (Release-Train, L3), z.B. ``1.4.0``."""

    issue_numbers: list[int] = Field(default_factory=list)
    """1.1: alle Work-Items, die dieses Release ausliefert (Train). Ein Item
    gilt als released, wenn es ``issue_number`` ist ODER hier steht."""

    integrated_into: str | None = None
    """1.1: das Item wurde nicht selbst released, sondern in einen
    Integrations-Branch (``forge/epic-<N>``) gemergt (G). Es ist damit fertig;
    ausgeliefert wird es mit dem Epic."""


# 1.1 (additiv): version, issue_numbers, integrated_into (Release-Train L3,
# Integrations-Branch G). Alte 1.0-Events lesen weiter.
register_payload(EventKind.RELEASE_TAGGED, ReleaseTaggedPayload, "1.1")
