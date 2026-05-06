"""Composite-Score-Berechnung.

Scores sind kontinuierliche Metriken, die zur Composite aggregiert werden —
**aber nur**, nachdem alle Gates grün sind (Spec Teil 5.1, 6.4).

Composite ist eine gewichtete Linearkombination, normalisiert pro Score auf
[0, 1] anhand einer optionalen Baseline. Lower-is-better-Scores werden invertiert,
sodass größer = besser für die Composite gilt.

Wenn keine Baseline vorhanden ist, fällt die Normalisierung auf eine
heuristische Skala zurück, die für die meisten Score-Kinds vernünftig ist —
das gilt nur für die erste Generation eines Runs.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_core.spec import ProjectSpec, ScoreConfig


@dataclass(frozen=True)
class ScoreBaseline:
    """Werte aus dem vorherigen `KEEP`-Zustand pro Score-Kind."""

    values: dict[str, float]

    def get(self, kind: str) -> float | None:
        return self.values.get(kind)


# Heuristische Maximalwerte für die initiale Generation, wenn keine Baseline da ist.
# Bewusst grob — der Composite ist für die erste Generation nur eine grobe Orientierung.
_HEURISTIC_SCALE: dict[str, float] = {
    "coverage_pct": 100.0,
    "p95_latency_ms": 1000.0,
    "bundle_kb": 1000.0,
    "ruff_warnings": 100.0,
    "todo_count": 100.0,
    "complexity_avg": 20.0,
    "test_count": 1000.0,
}


def compute_composite(
    *,
    measurements: dict[str, float],
    spec: ProjectSpec,
    baseline: ScoreBaseline | None = None,
) -> float | None:
    """Berechnet den gewichteten Composite-Score.

    Liefert `None`, wenn keine Scores in der Spec definiert sind oder kein
    einziger Score gemessen wurde.

    Werte werden auf [0, 1] normalisiert:
      - higher_is_better: actual / scale
      - lower_is_better:  1 - (actual / scale), abgeschnitten auf [0, 1]

    Wenn `baseline` einen Wert für einen Score-Kind hat, wird dieser als Skala
    verwendet (der Score relativ zur Baseline). Sonst Heuristik aus
    `_HEURISTIC_SCALE`.
    """
    if not spec.scores:
        return None

    weighted_sum = 0.0
    used_weight = 0.0

    for score in spec.scores:
        actual = measurements.get(score.kind)
        if actual is None:
            continue
        normalized = _normalize(score, actual, baseline)
        weighted_sum += normalized * score.weight
        used_weight += score.weight

    if used_weight == 0:
        return None

    # Skaliere auf das benutzte Gewicht — falls einzelne Scores fehlen, soll der
    # Composite trotzdem in [0, 1] bleiben.
    return weighted_sum / used_weight


def _normalize(
    score: ScoreConfig,
    actual: float,
    baseline: ScoreBaseline | None,
) -> float:
    """Normalisiert einen Einzelwert auf [0, 1]."""
    scale: float | None = None
    if baseline is not None:
        prior = baseline.get(score.kind)
        if prior is not None and prior > 0:
            scale = prior * 2.0  # Baseline = 0.5; doppelt so gut = 1.0

    if scale is None:
        scale = _HEURISTIC_SCALE.get(score.kind)

    if scale is None or scale <= 0:
        # Kein sinnvoller Skalierungsmaßstab — als 0.5 normalisieren (neutral)
        return 0.5

    if score.lower_is_better:
        ratio = actual / scale
        # 0 = perfekt, scale = mittelmäßig (0.5), 2*scale = schlecht (0)
        return max(0.0, min(1.0, 1.0 - ratio / 2.0))
    else:
        return max(0.0, min(1.0, actual / scale))


def keep_or_discard(
    *,
    new_composite: float | None,
    baseline_composite: float | None,
    tolerance: float = 0.02,
) -> tuple[bool, str]:
    """Keep/Discard-Logik aus Spec Teil 6.5.

    Wird vom Runner nach erfolgreichen Gates aufgerufen.

    Returns:
        (keep, reason) — `reason` ∈ {"improvement", "no_significant_change", "regression"}
    """
    if new_composite is None:
        return False, "no_significant_change"
    if baseline_composite is None:
        # Erste Generation: alles >0 zählt als Improvement
        return new_composite > 0, "improvement" if new_composite > 0 else "no_significant_change"

    delta = new_composite - baseline_composite
    if delta > tolerance:
        return True, "improvement"
    if delta > -tolerance:
        return False, "no_significant_change"
    return False, "regression"
