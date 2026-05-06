"""Gate-Evaluation.

Gates sind harte Pass/Fail-Kriterien. Eine Generation, die ein Gate nicht
erfüllt, wird **nie behalten**, egal wie hoch ihr Composite-Score liegt
(Spec Teil 5.1).

Diese Datei stellt die reine Logik bereit: gegeben ein dict aller Eval-Outputs
und die Gate-Liste der Spec, liefert sie zurück, welche Gates passen.
"""

from __future__ import annotations

from dataclasses import dataclass

from forge_core.events import EvalFinishedPayload
from forge_core.events.kinds.eval_ import GateResult
from forge_core.spec import GateConfig, ProjectSpec


@dataclass(frozen=True)
class GateBaseline:
    """Werte aus dem vorherigen `KEEP`-Run pro Gate-Kind.

    Wird für `max_increase`-Gates gebraucht (z.B. mypy_errors darf nicht
    steigen). Bei Run-Start ist die Baseline der Wert vom HEAD vor der Mutation.
    """

    values: dict[str, float]

    def get(self, kind: str) -> float | None:
        return self.values.get(kind)


def evaluate_gates(
    *,
    measurements: dict[str, float],
    spec: ProjectSpec,
    baseline: GateBaseline | None = None,
) -> tuple[bool, list[GateResult]]:
    """Wertet alle Gates aus.

    Args:
        measurements: Eval-Output, geparst zu numerischen Werten pro Gate-Kind.
            Schlüssel müssen den `kind`-Werten in `spec.gates` entsprechen.
        spec: Die Project-Spec.
        baseline: Werte vom letzten `KEEP`-Run für `max_increase`-Gates.

    Returns:
        (all_passed, list[GateResult]) — `all_passed` ist die UND-Verknüpfung
        aller Einzelergebnisse, `GateResult`-Liste hat einen Eintrag pro Gate
        in spec-Reihenfolge.
    """
    results: list[GateResult] = []
    for gate in spec.gates:
        results.append(_eval_one(gate, measurements, baseline))
    return all(r.passed for r in results), results


def _eval_one(
    gate: GateConfig,
    measurements: dict[str, float],
    baseline: GateBaseline | None,
) -> GateResult:
    actual = measurements.get(gate.kind)

    if actual is None:
        return GateResult(
            kind=gate.kind,
            passed=False,
            actual=None,
            threshold=gate.threshold,
            reason=f"no measurement for kind {gate.kind!r}",
        )

    # max_increase: Wert darf gegenüber Baseline nicht steigen
    if gate.max_increase is not None:
        if baseline is None:
            return GateResult(
                kind=gate.kind,
                passed=False,
                actual=actual,
                reason="max_increase gate needs baseline, none given",
            )
        prior = baseline.get(gate.kind)
        if prior is None:
            # Kein Vorwert — als Pass durchwinken; erste Generation
            return GateResult(
                kind=gate.kind,
                passed=True,
                actual=actual,
                reason="no baseline yet, accepting initial value",
            )
        passed = (actual - prior) <= gate.max_increase
        return GateResult(
            kind=gate.kind,
            passed=passed,
            actual=actual,
            threshold=prior + gate.max_increase,
            reason=None if passed else f"increased by {actual - prior:.3g}",
        )

    # threshold-basiert
    assert gate.threshold is not None  # via spec-Validator garantiert
    if gate.lower_is_better:
        passed = actual <= gate.threshold
        reason = None if passed else f"actual {actual:.3g} > threshold {gate.threshold:.3g}"
    else:
        passed = actual >= gate.threshold
        reason = None if passed else f"actual {actual:.3g} < threshold {gate.threshold:.3g}"

    return GateResult(
        kind=gate.kind,
        passed=passed,
        actual=actual,
        threshold=gate.threshold,
        reason=reason,
    )


def attach_to_eval_payload(
    base: EvalFinishedPayload,
    gates: list[GateResult],
) -> EvalFinishedPayload:
    """Liefert einen neuen `EvalFinishedPayload` mit gesetzten Gate-Resultaten.

    `EvalFinishedPayload` ist Pydantic-immutable, also `model_copy`.
    """
    all_passed = all(g.passed for g in gates)
    return base.model_copy(
        update={
            "gates": gates,
            "gates_passed": all_passed,
            # composite_value bleibt unberührt; Caller setzt es nur, wenn passed.
        }
    )
