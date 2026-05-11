"""Loader und Validator für `.forge/project.yaml`.

`project.yaml` ist der Vertrag zwischen Operator und Maschine (Spec Teil 5).
Drei Dinge sind hier verpflichtend scharf:

1. **Surfaces dürfen sich nicht überlappen** — sonst ist die Pfad→Surface-Zuordnung
   nicht-deterministisch.
2. **`forbidden` schlägt `surfaces`** — falls ein Pfad in beiden auftaucht, ist das
   ein Spec-Fehler. Konkret: kein `forbidden`-Pattern darf gleichzeitig in einem
   Surface enthalten sein (Spec Teil 5.2).
3. **`merge_pr` und `push_to_main` MÜSSEN false sein** — hardcoded in v1, kein
   Override (Spec Teil 5.2). Die Spec selbst enthält die Felder explizit, damit
   sie sichtbar als deaktiviert dokumentiert sind.

Score-Gewichte werden auf Summe=1.0 normalisiert, mit Warning falls Eingabe
abweicht (siehe Doc von `ProjectSpec.normalize_score_weights`).
"""

from __future__ import annotations

import warnings
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)


class SpecValidationError(ValueError):
    """Spec ist syntaktisch ok, aber inhaltlich ungültig."""


# --- Enums --------------------------------------------------------------

GateKind = Literal[
    "pytest_pass_rate",
    "ruff_warnings",
    "bandit_high",
    "tsc_errors",
    "mypy_errors",
    "eslint_errors",
    "coverage_pct",
    "build_success",
]

ScoreKind = Literal[
    "coverage_pct",
    "p95_latency_ms",
    "bundle_kb",
    "ruff_warnings",
    "todo_count",
    "complexity_avg",
    "test_count",
]

DiagnosticKind = Literal[
    "llm_judge_score",
    "complexity_avg",
    "import_count",
    "todo_count",
]


class SurfaceType(StrEnum):
    CODE = "code"
    YAML_KEYS = "yaml-keys"
    TEXT = "text"


# --- Sub-Modelle --------------------------------------------------------

NonEmptyStr = Annotated[str, StringConstraints(min_length=1)]


class SurfaceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[NonEmptyStr] = Field(min_length=1)
    type: SurfaceType
    guardrails: list[NonEmptyStr] = Field(default_factory=list)
    """Schnelle Sanity-Checks (lint, syntax, schmaler Pytest). Budget: <30s."""

    allowed_keys: list[NonEmptyStr] | None = None
    """Pflicht für `type: yaml-keys`."""

    @model_validator(mode="after")
    def _check_yaml_keys_has_allowed(self) -> SurfaceConfig:
        if self.type is SurfaceType.YAML_KEYS and not self.allowed_keys:
            raise SpecValidationError(
                "surface type 'yaml-keys' requires `allowed_keys`"
            )
        return self


class CapabilitiesConfig(BaseModel):
    """Aktions-Erlaubnisse für die Maschine (Spec Teil 5.2).

    `merge_pr`, `push_to_main` und `push_force` sind in v1 hardcoded `false`.
    Eine spec, die sie auf `true` setzt, wird abgelehnt.
    """

    model_config = ConfigDict(extra="forbid")

    read: list[NonEmptyStr] = Field(default_factory=lambda: ["**/*"])
    edit: list[NonEmptyStr] = Field(default_factory=lambda: ["{surfaces}"])
    run: list[NonEmptyStr] = Field(default_factory=list)

    commit: bool = True
    open_pr: bool = True
    comment_issue: bool = True
    """Erlaubt ``gh issue comment`` (z.B. für Triage-Begründungen).
    Default ``True`` — Kommentare sind weniger invasiv als ein PR-Open."""
    close_issue: bool = True
    """Erlaubt ``gh issue close`` (z.B. um veraltete Issues per Triage
    zu schließen). Default ``True``, aber praktisch nur wirksam, wenn
    ``spec.triage.enabled`` opt-in aktiviert ist."""
    merge_pr: Literal[False] = False
    push_to_main: Literal[False] = False
    push_force: Literal[False] = False

    network_egress: list[NonEmptyStr] = Field(
        default_factory=lambda: ["api.anthropic.com", "api.github.com"]
    )

    @field_validator("merge_pr", "push_to_main", "push_force", mode="before")
    @classmethod
    def _reject_true(cls, value: Any, info: Any) -> Any:
        if value is True:
            raise SpecValidationError(
                f"capabilities.{info.field_name} must be false in v1 — "
                "auto-merge / push-to-main / push-force are categorically disabled "
                "(Spec Teil 5.2, 7.5)"
            )
        return value


class EvalSuiteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cmd: NonEmptyStr
    budget_s: int = Field(gt=0)
    parses: Literal["pytest_json", "scores_json", "raw"] = "raw"


class GateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: GateKind
    threshold: float | None = None
    max_increase: float | None = None
    lower_is_better: bool = False
    source: str | None = None
    """Eval-Suite, aus der das Signal kommt (z.B. 'quick', 'full')."""

    @model_validator(mode="after")
    def _exactly_one_criterion(self) -> GateConfig:
        if self.threshold is None and self.max_increase is None:
            raise SpecValidationError(
                f"gate {self.kind!r}: must specify either `threshold` or `max_increase`"
            )
        return self


class ScoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ScoreKind
    weight: float = Field(gt=0.0, le=1.0)
    lower_is_better: bool = False
    source: str | None = None


class DiagnosticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: DiagnosticKind
    source: str | None = None
    scenarios: str | None = None


class CostCapsConfig(BaseModel):
    """Vier Ebenen, alle Pflicht ab Tag eins (Spec Teil 5.3)."""

    model_config = ConfigDict(extra="forbid")

    per_generation_usd: Decimal
    per_run_usd: Decimal
    per_project_per_day_usd: Decimal
    per_project_per_month_usd: Decimal

    @model_validator(mode="after")
    def _monotone(self) -> CostCapsConfig:
        if not (
            self.per_generation_usd
            <= self.per_run_usd
            <= self.per_project_per_day_usd
            <= self.per_project_per_month_usd
        ):
            raise SpecValidationError(
                "cost_caps must be monotone: generation <= run <= project_per_day "
                "<= project_per_month"
            )
        return self


class TriggerStrategy(StrEnum):
    SEQUENTIAL = "sequential"
    REVIEW_ONLY = "review_only"


# Subagent-Namen, die Spec v0.3 Teil 6.5 als Pflicht/Optional definiert.
# v1: architect, developer, tester. v1.5+: reviewer, operations.
SubagentName = Literal["architect", "developer", "tester", "reviewer", "operations"]


class IssueLabelTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: TriggerStrategy
    model: str
    max_iterations: int | None = Field(default=None, gt=0)
    requires_human_review: bool = False
    agents: list[SubagentName] = Field(
        default_factory=lambda: ["architect", "developer", "tester"]
    )


class PROpenedTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: TriggerStrategy = TriggerStrategy.REVIEW_ONLY
    model: str
    # Default: Reviewer-Only für PR-Review-Trigger
    agents: list[SubagentName] = Field(default_factory=lambda: ["reviewer"])


class CIFailureTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy: TriggerStrategy = TriggerStrategy.SEQUENTIAL
    model: str
    max_iterations: int = Field(default=3, gt=0)
    # CI-Fix ist eng — single-agent reicht, kein Plan-Mode nötig
    agents: list[SubagentName] = Field(default_factory=lambda: ["developer"])


class ScheduleTriggerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cron: NonEmptyStr
    strategy: TriggerStrategy = TriggerStrategy.SEQUENTIAL
    focus: NonEmptyStr
    model: str | None = None
    max_iterations: int | None = Field(default=None, gt=0)
    agents: list[SubagentName] = Field(
        default_factory=lambda: ["architect", "developer", "tester"]
    )


class TriggersConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    on_issue_label: dict[NonEmptyStr, IssueLabelTriggerConfig] = Field(default_factory=dict)
    on_pr_opened: PROpenedTriggerConfig | None = None
    on_ci_failure: CIFailureTriggerConfig | None = None
    schedule: list[ScheduleTriggerConfig] = Field(default_factory=list)


class ReleaseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conventional_commits: bool = True
    on_main_green: Literal["auto_tag", "off"] = "off"
    """Tagging ist OK (kein Code-Change). Merge ist es nicht."""

    changelog: NonEmptyStr | None = None


class TriageConfig(BaseModel):
    """LLM-gestützte Pre-Phase im board-loop (Spec v0.4 Teil 6.3).

    Bevor forge einen Worktree öffnet und den vollen 5-Phasen-Zyklus
    durchläuft, klassifiziert der Triager jedes ready-Issue gegen den
    aktuellen ``main``-Stand und die offenen PRs/Issues. Mögliche
    Outcomes:

    * ``relevant`` — normaler Dispatch, kein Side-Effect.
    * ``stale`` — das Issue beschreibt einen längst behobenen Zustand
      oder bezieht sich auf Code, der nicht mehr existiert.
    * ``duplicate`` — ein offenes Issue/PR adressiert dasselbe Problem.
    * ``already_solved`` — der Fix ist bereits im ``main`` gelandet.

    In allen "nicht relevant"-Fällen werden — wenn aktiviert — Begründung
    als Kommentar gespiegelt und das Issue geschlossen. Genau eine
    ``IssueTriaged``-Event-Zeile fällt pro Issue an, sodass spätere
    Loop-3-Auswertungen Trefferrate und Drift messen können.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    """Schaltet die Pre-Phase im board-loop ein. Default ``False`` —
    Triage ist Opt-in, weil sie zusätzliche LLM-Kosten pro Issue
    produziert (kleiner Call, aber nicht null)."""

    model: str | None = None
    """Claude-Modell für den Triage-Call. ``None`` = nutze das Modell
    aus dem ``issue_label``-Trigger der Spec. Ein kleines Modell
    reicht hier oft (Klassifikation, kein Code)."""

    max_turns: int = Field(default=4, gt=0)
    """Cap auf Tool-Turns im Triage-Aufruf. Default 4 reicht für
    ``gh issue list`` + ``gh pr list`` + ``git log`` + Decision."""

    auto_comment: bool = True
    """Bei ``decision != relevant``: Begründung als Issue-Kommentar
    posten. Benötigt ``capabilities.comment_issue``."""

    auto_close: bool = True
    """Bei ``decision != relevant``: Issue schließen. Benötigt
    ``capabilities.close_issue``. Auf ``False`` setzen, wenn man
    nur den Kommentar will und manuell schließen möchte."""


class BoardConfig(BaseModel):
    """GitHub Project board als aktive Trigger-Quelle (Spec v0.4).

    Wird von `forge board-loop` konsumiert: poll das Board, filtere nach
    Status + Labels, dispatche pro ready-Issue einen normalen
    `forge run --trigger issue_label`. Forges 5-Phasen-Pipeline und
    Capabilities-Vertrag bleiben unangetastet — `BoardConfig` ist reine
    Trigger-Source-Konfiguration, nicht Loop-Logik.

    Idempotenz wird im Adapter gemacht (Skip wenn offener PR mit
    `Closes #N` existiert), nicht hier.
    """

    model_config = ConfigDict(extra="forbid")

    provider: Literal["github"] = "github"
    """In v1 nur github. ``provider`` ist explizit, damit spätere GitLab-/
    Linear-Adapter additiv werden statt breaking."""

    owner: NonEmptyStr
    """GitHub user oder org, der das Project besitzt."""

    project_number: int = Field(gt=0)
    """Project (v2) Nummer — die ID aus ``gh project list``."""

    filter_status: NonEmptyStr = "Todo"
    """Status-Field-Wert, der als 'ready to dispatch' zählt. Case-sensitiv,
    muss exakt einem Status-Field-Optionsnamen im Project entsprechen."""

    filter_labels: list[NonEmptyStr] = Field(default_factory=lambda: ["bug"])
    """Issue muss ALLE gelisteten Labels tragen (AND). Leere Liste = kein
    Label-Filter (alle Labels akzeptiert)."""

    default_focus_template: NonEmptyStr = "issue-{number}"
    """Focus-Tag pro dispatched Run. ``{number}`` wird durch issue.number
    substituiert. Wirkt als Telemetrie-Schlüssel in den Events."""

    default_template_id: NonEmptyStr = "board_loop_v1"
    """``prompt_template_id`` für jeden vom board-loop dispatched Run.
    Macht in `forge analyze`-Reports board-loop-Runs unterscheidbar."""


# --- Top-Level ----------------------------------------------------------


class ProjectSpec(BaseModel):
    """Vollständige `.forge/project.yaml`-Spec.

    Alle harten Verträge (überlappende Surfaces, forbidden vs. surfaces,
    capability-Hardcodes, Score-Weights) werden hier validiert.
    """

    model_config = ConfigDict(extra="forbid")

    spec_version: NonEmptyStr
    name: NonEmptyStr
    language_stack: list[NonEmptyStr] = Field(default_factory=list)
    frameworks: list[NonEmptyStr] = Field(default_factory=list)

    surfaces: dict[NonEmptyStr, SurfaceConfig] = Field(default_factory=dict)
    forbidden: list[NonEmptyStr] = Field(default_factory=list)
    capabilities: CapabilitiesConfig = Field(default_factory=CapabilitiesConfig)
    eval_suites: dict[NonEmptyStr, EvalSuiteConfig] = Field(default_factory=dict)

    gates: list[GateConfig] = Field(default_factory=list)
    scores: list[ScoreConfig] = Field(default_factory=list)
    diagnostics: list[DiagnosticConfig] = Field(default_factory=list)

    cost_caps: CostCapsConfig
    triggers: TriggersConfig = Field(default_factory=TriggersConfig)
    release: ReleaseConfig = Field(default_factory=ReleaseConfig)
    board: BoardConfig | None = None
    """Optionaler Project-Board-Config; aktiviert ``forge board-loop`` (v0.4)."""
    triage: TriageConfig = Field(default_factory=TriageConfig)
    """Pre-Phase im board-loop. Default: disabled. Siehe :class:`TriageConfig`."""

    # --- Cross-field validation ----------------------------------------

    @model_validator(mode="after")
    def _surfaces_dont_overlap(self) -> ProjectSpec:
        seen_paths: dict[str, str] = {}  # path -> surface_name
        for surf_name, surf in self.surfaces.items():
            for p in surf.paths:
                if p in seen_paths:
                    raise SpecValidationError(
                        f"surface path overlap: {p!r} appears in both "
                        f"surfaces.{seen_paths[p]} and surfaces.{surf_name}"
                    )
                seen_paths[p] = surf_name
        return self

    @model_validator(mode="after")
    def _forbidden_not_in_surfaces(self) -> ProjectSpec:
        """Forbidden-Pfade dürfen nicht in einer Surface deklariert sein.

        Wir prüfen exakte String-Identität — Glob-Overlap (z.B. surface
        `backend/src/**` plus forbidden `backend/src/routes/auth.py`) ist
        gewollt erlaubt: forbidden ist die feinere Auflösung. Die Pflicht
        ist nur, dass derselbe literale Pfad nicht in beiden steht.
        """
        surface_paths: set[str] = set()
        for surf in self.surfaces.values():
            surface_paths.update(surf.paths)
        for fp in self.forbidden:
            if fp in surface_paths:
                raise SpecValidationError(
                    f"forbidden path {fp!r} is also declared in a surface — "
                    f"forbidden takes precedence; remove the explicit surface entry"
                )
        return self

    @model_validator(mode="after")
    def _gates_reference_existing_suites(self) -> ProjectSpec:
        suites = set(self.eval_suites)
        for g in self.gates:
            if g.source is not None and g.source not in suites:
                raise SpecValidationError(
                    f"gate {g.kind!r} references unknown eval_suite {g.source!r}; "
                    f"known suites: {sorted(suites)}"
                )
        for s in self.scores:
            if s.source is not None and s.source not in suites:
                raise SpecValidationError(
                    f"score {s.kind!r} references unknown eval_suite {s.source!r}; "
                    f"known suites: {sorted(suites)}"
                )
        for diag in self.diagnostics:
            if diag.source is not None and diag.source not in suites:
                raise SpecValidationError(
                    f"diagnostic {diag.kind!r} references unknown eval_suite "
                    f"{diag.source!r}; known suites: {sorted(suites)}"
                )
        return self

    @model_validator(mode="after")
    def _normalize_score_weights(self) -> ProjectSpec:
        if not self.scores:
            return self
        total = sum(s.weight for s in self.scores)
        if total <= 0:
            raise SpecValidationError("sum of score weights must be > 0")
        if abs(total - 1.0) > 1e-6:
            warnings.warn(
                f"score weights sum to {total:.6f}, normalizing to 1.0",
                stacklevel=2,
            )
            for s in self.scores:
                # Pydantic v2: model is mutable until validation completes
                object.__setattr__(s, "weight", s.weight / total)
        return self

    @model_validator(mode="after")
    def _required_evals_present(self) -> ProjectSpec:
        # Falls überhaupt Eval-Suiten konfiguriert sind, soll `quick` dabei sein
        # (siehe Spec Teil 6.4 — `quick` läuft IMMER).
        if self.eval_suites and "quick" not in self.eval_suites:
            raise SpecValidationError(
                "eval_suites must include a 'quick' suite (Spec Teil 6.4)"
            )
        return self

    # --- Lookup helpers -------------------------------------------------

    def surface_for_path(self, path: str) -> tuple[str, SurfaceConfig] | None:
        """Liefert (surface_name, config), wenn `path` in einer Surface liegt.

        Vergleich ist String-Prefix auf normalisiertem POSIX-Pfad — ausreichend
        für die in der Spec gezeigten Pattern. Glob-Matching kommt im Capability-
        Enforcer in `forge-execute`.
        """
        norm = path.replace("\\", "/")
        for surf_name, surf in self.surfaces.items():
            for p in surf.paths:
                p_norm = p.replace("\\", "/")
                if norm.startswith(p_norm):
                    return surf_name, surf
        return None


# --- I/O ---------------------------------------------------------------


def load_spec(path: Path | str) -> ProjectSpec:
    """Lädt eine `.forge/project.yaml` und validiert sie streng."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise SpecValidationError(f"{p}: top-level must be a mapping, got {type(raw).__name__}")
    return ProjectSpec.model_validate(raw)


def dump_spec(spec: ProjectSpec, path: Path | str) -> None:
    """Schreibt eine Spec als YAML zurück (Round-Trip-Test, `forge init`)."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = spec.model_dump(mode="json", exclude_none=False)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
