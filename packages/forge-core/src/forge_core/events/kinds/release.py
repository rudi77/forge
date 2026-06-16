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


register_payload(EventKind.RELEASE_TAGGED, ReleaseTaggedPayload, "1.0")
