"""Sequential Runner — Loop 1.

Eine Generation = Propose → Mutate → Preflight → Eval → Decide (Spec Teil 6).
Cost-Cap-Check vor jedem LLM-Call (Spec Teil 5.3, 7.1). Alle Events werden
in den Store geschrieben; Artefakte (Prompt, Diff, Eval-Output, Tool-Versionen)
in den Blob-Store.

Designentscheidungen für M1:

- Nur eine Eval-Suite pro Run, default `quick`. `full` + `judge` sind M2.
- Cost-Caps der Ebenen `generation` und `run`. `project_day`/`project_month`
  sind v1-Stubs (Query gegen den Store) — können in M2 nachgezogen werden.
- Kein PR-Erzeugung hier — das macht `forge-adapters/github`. Der Runner
  liefert das letzte gehaltene Commit-SHA + Diff zurück.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from forge_core.blobs import BlobStore
from forge_core.events import (
    EventKind,
    GenerationFinishedPayload,
    GenerationStartedPayload,
    GuardrailViolationPayload,
    MutationAppliedPayload,
    PreflightFailedPayload,
    ProposalReceivedPayload,
    ProposalRequestedPayload,
    RunFinishedPayload,
    RunStartedPayload,
    build_event,
)
from forge_core.events.kinds.cost import CostCapHitPayload
from forge_core.events.kinds.decision import DecisionMadePayload
from forge_core.events.kinds.eval_ import EvalFinishedPayload, EvalStartedPayload
from forge_core.events.kinds.run import TriggerKind
from forge_core.spec import ProjectSpec
from forge_core.store import EventStore
from ulid import ULID

from forge_execute.agents.base import (
    CodingAgent,
    CodingAgentError,
    CodingAgentTimeout,
    ProposalResult,
)
from forge_execute.capabilities import Capabilities
from forge_execute.evaluators.command import CommandEvaluator, EvalRunResult
from forge_execute.evaluators.judge import JudgeEvaluator, JudgeOutcome
from forge_execute.gates import GateBaseline, evaluate_gates
from forge_execute.mutators.code import CodeMutator, MutationResult
from forge_execute.scoring import ScoreBaseline, compute_composite, keep_or_discard
from forge_execute.worktrees import Worktree, WorktreeManager

# --- Run-Config + Result -------------------------------------------------


@dataclass
class RunConfig:
    """Eingabe für einen Run.

    `prompt_template_id` ist die stabile ID des Prompt-Templates (z.B.
    `fix_failing_test_v3`); `initial_prompt` der gerenderte Text. Beide werden
    geloggt.
    """

    spec: ProjectSpec
    project: str
    project_fingerprint: str
    factory_version: str
    repo_root: Path

    prompt_template_id: str
    initial_prompt: str

    acceptance_criteria: str | None = None
    """Kriterien, gegen die der LLM-Judge den Diff bewertet (Spec v0.5).
    ``None`` = nutze ``initial_prompt`` (der Issue-Text ist das Kriterium).
    Nur relevant, wenn ``spec.judge.enabled``."""

    trigger: TriggerKind = "manual"
    focus: str | None = None
    base_ref: str = "HEAD"
    max_iterations: int = 10
    tolerance: float = 0.02
    eval_suite: str = "quick"
    model: str | None = None
    max_turns_per_proposal: int = 8
    issue_number: int | None = None
    pr_number: int | None = None

    agents: list[str] = field(
        default_factory=lambda: ["architect", "developer", "tester"]
    )
    """Aktiviertes Subagent-Roster (Spec v0.3 Teil 5.1). Wird in das
    PlanProposed-Event als `agents_used` gespiegelt — der Record, welche
    Arbeitspferde am Run mitwirkten."""


@dataclass
class GenerationOutcome:
    """Pro-Generation-Zusammenfassung, vom Runner intern geführt.

    `error` ist ein Aufruf-übergreifender Marker:
    - `"guardrail"` — Run abbrechen (Capability-Verletzung)
    - `"cost_cap_hit"` — Run abbrechen (Generation-Cost-Cap)
    - `"self_terminated"` — Run abbrechen (Agent: nothing more to do)
    - sonst None
    """

    idx: int
    kept: bool
    reason: str
    composite: float | None = None
    score_delta: float | None = None
    cost_usd: Decimal = Decimal("0")
    error: str | None = None


@dataclass
class RunResult:
    """Endergebnis eines Runs."""

    run_id: str
    decision: str
    generations: list[GenerationOutcome] = field(default_factory=list)
    final_score: float | None = None
    score_delta: float | None = None
    total_cost_usd: Decimal = Decimal("0")
    branch: str | None = None
    final_commit: str | None = None
    final_diff: str | None = None


# --- Runner --------------------------------------------------------------


class SequentialRunner:
    """Führt einen Run mit `strategy: sequential` durch (Spec Teil 6)."""

    def __init__(
        self,
        *,
        config: RunConfig,
        agent: CodingAgent,
        store: EventStore,
        blobs: BlobStore,
        worktrees: WorktreeManager | None = None,
    ) -> None:
        self.config = config
        self.agent = agent
        self.store = store
        self.blobs = blobs
        self.worktrees = worktrees or WorktreeManager(config.repo_root)
        self.capabilities = Capabilities(config.spec)
        self.mutator = CodeMutator(
            worktrees=self.worktrees, capabilities=self.capabilities
        )
        self.evaluator = CommandEvaluator()
        # Judge-Phase (Spec v0.5): opt-in via spec.judge.enabled. Wenn aus,
        # bleibt der Pfad exakt wie vorher — kein Judge-Call, keine Kosten.
        self._judge = (
            JudgeEvaluator(agent) if config.spec.judge.enabled else None
        )

        self.run_id = str(ULID())
        self._spec_version = config.spec.spec_version
        self._common = dict(
            project=config.project,
            project_fingerprint=config.project_fingerprint,
            factory_version=config.factory_version,
            spec_version=self._spec_version,
        )

        # Mutable Run-State
        self._total_cost = Decimal("0")
        self._gate_baseline: GateBaseline | None = None
        self._score_baseline: ScoreBaseline | None = None
        self._baseline_composite: float | None = None
        self._baseline_gates_passed: bool = True
        # Pro-Gate Baseline-Resultate (Spec v0.3 Teil 5.2 — strikte
        # Erhaltung: keine vorher-grüne Gate darf rot werden).
        self._baseline_gate_results: dict[str, bool] | None = None
        self._kept_outcomes: list[GenerationOutcome] = []
        self._all_outcomes: list[GenerationOutcome] = []
        # Aktueller Commit, auf den DISCARD-Generations zurückrollen.
        # Bei Run-Start = worktree.base_commit; nach jeder KEEP-Generation
        # auf den dann commitierten HEAD aktualisiert. Sonst würde ein
        # DISCARD nach einem KEEP die KEPT-Commits wegblasen.
        self._current_base_commit: str | None = None

        # Inter-Generation-Memory (Spec v0.3 Teil 6.8): akkumulierte
        # KEPT-Diffs werden in den Prompt der nächsten Generation gespliced.
        from forge_execute._run_context import RunContext

        self._run_context = RunContext()

    # --- Top-level run ---------------------------------------------------

    def run(self) -> RunResult:
        worktree = self.worktrees.create(
            run_id=self.run_id,
            base_ref=self.config.base_ref,
        )
        try:
            return self._run_in_worktree(worktree)
        finally:
            # Worktree bleibt liegen, falls erfolgreich (PR-Erzeugung später)
            # -- Discard-Pfad: cleanup im finally des Runs erfolgt, wenn kein
            # Branch zum PRen übrig bleibt.
            pass

    def _run_in_worktree(self, worktree: Worktree) -> RunResult:
        # Initialer revert-Anker = base_commit. Wird nach jeder KEEP-Generation
        # auf den dann committeten HEAD aktualisiert.
        self._current_base_commit = worktree.base_commit

        # --- Initiale Baseline aus dem Worktree-Stand ableiten ----------
        # In v1 ist das HEAD vor jeder Mutation; spätere Iterationen aktualisieren
        # die Baseline pro KEEP.
        baseline_eval = self._eval_baseline(worktree)
        self._update_baselines(baseline_eval)

        # --- RunStarted ------------------------------------------------
        config_hash = self._compute_config_hash()
        self._emit(
            EventKind.RUN_STARTED,
            RunStartedPayload(
                trigger=self.config.trigger,
                strategy="sequential",
                config_hash=config_hash,
                baseline_metrics=dict(baseline_eval.measurements),
                focus=self.config.focus,
                issue_number=self.config.issue_number,
                pr_number=self.config.pr_number,
                max_iterations=self.config.max_iterations,
            ),
        )

        # --- Generationen ---------------------------------------------
        decision = "no_improvement"
        for gen_idx in range(self.config.max_iterations):
            cap_hit = self._check_run_cost_cap()
            if cap_hit is not None:
                decision = "cost_cap_hit"
                break

            outcome = self._run_one_generation(worktree, gen_idx)
            self._all_outcomes.append(outcome)
            if outcome.kept:
                self._kept_outcomes.append(outcome)

            if outcome.error == "guardrail":
                decision = "guardrail_blocked"
                break
            if outcome.error == "cost_cap_hit":
                decision = "cost_cap_hit"
                break
            if outcome.error == "self_terminated":
                # Agent meldete "nothing more to do" — Run ist vollständig
                # (Spec v0.3 Teil 6.8). Wenn KEPT-Generations existieren,
                # endet der Run weiter unten als pr_created; sonst als
                # explizit self_terminated.
                if not self._kept_outcomes:
                    decision = "self_terminated"
                break

        # --- Abschluss --------------------------------------------------
        if decision == "no_improvement" and self._kept_outcomes:
            decision = "pr_created"  # Caller wandelt das in den eigentlichen PR um

        final_score = (
            self._kept_outcomes[-1].composite if self._kept_outcomes else None
        )
        score_delta = (
            self._kept_outcomes[-1].composite - self._baseline_composite
            if self._kept_outcomes
            and self._kept_outcomes[-1].composite is not None
            and self._baseline_composite is not None
            else None
        )

        diff = self.worktrees.diff_against_base(worktree) if self._kept_outcomes else None
        final_commit = (
            self.worktrees._rev_parse("HEAD", cwd=worktree.path)
            if self._kept_outcomes
            else None
        )

        self._emit(
            EventKind.RUN_FINISHED,
            RunFinishedPayload(
                decision=decision,
                generations_count=len(self._all_outcomes),
                final_score=final_score,
                baseline_score=self._baseline_composite,
                score_delta=score_delta,
                total_cost_usd=self._total_cost,
                pr_number=self.config.pr_number,
            ),
        )

        return RunResult(
            run_id=self.run_id,
            decision=decision,
            generations=list(self._all_outcomes),
            final_score=final_score,
            score_delta=score_delta,
            total_cost_usd=self._total_cost,
            branch=worktree.branch if self._kept_outcomes else None,
            final_commit=final_commit,
            final_diff=diff,
        )

    # --- Generation ------------------------------------------------------

    def _run_one_generation(
        self,
        worktree: Worktree,
        gen_idx: int,
    ) -> GenerationOutcome:
        gen_id = str(ULID())

        self._emit(
            EventKind.GENERATION_STARTED,
            GenerationStartedPayload(
                generation_idx=gen_idx,
                parent_score=self._baseline_composite,
                focus=self.config.focus,
            ),
            generation_id=gen_id,
        )

        # --- Phase 1: Propose ----------------------------------------
        proposal = self._propose(worktree, gen_id, gen_idx)
        if proposal is None:
            outcome = GenerationOutcome(
                idx=gen_idx, kept=False, reason="propose_failed", error="error"
            )
            self._finish_generation(gen_id, outcome)
            return outcome

        # Cost-Cap pro Generation prüfen, NACHDEM Proposal-Cost feststehen
        if proposal.cost_usd > self.config.spec.cost_caps.per_generation_usd:
            self._emit(
                EventKind.COST_CAP_HIT,
                CostCapHitPayload(
                    level="generation",
                    cap_usd=self.config.spec.cost_caps.per_generation_usd,
                    actual_usd=proposal.cost_usd,
                ),
                generation_id=gen_id,
            )
            self.worktrees.revert(worktree, to_commit=self._current_base_commit)
            outcome = GenerationOutcome(
                idx=gen_idx,
                kept=False,
                reason="cost_cap_hit",
                cost_usd=proposal.cost_usd,
                error=None,
            )
            self._finish_generation(gen_id, outcome)
            return outcome

        # Self-Termination-Marker MERKEN, aber Generation NICHT prematurely
        # beenden: wenn der Agent bereits Änderungen gemacht hat, müssen die
        # noch durch Validate/Eval/Decide laufen — sonst geht der KEEP-commit
        # verloren. Der `error="self_terminated"`-Marker wird am Generation-
        # Outcome durchgereicht, der Run-Loop bricht später ab.
        is_self_terminated = proposal.error == "self_terminated"

        if not proposal.has_changes:
            # Keine Änderungen UND Signal? → eindeutig "fertig", kein KEEP nötig.
            # Keine Änderungen OHNE Signal? → leerer Vorschlag (z.B. Plan ohne
            # Implementierung), regulärer DISCARD ohne Run-Abort.
            outcome = GenerationOutcome(
                idx=gen_idx,
                kept=False,
                reason="self_terminated" if is_self_terminated else "empty_proposal",
                cost_usd=proposal.cost_usd,
                error="self_terminated" if is_self_terminated else None,
            )
            self._finish_generation(gen_id, outcome)
            return outcome

        # --- Phase 2: Mutate (Validierung der schon vom Agent applizierten
        #     Änderungen — keine Re-Application, weil der Agent die Files
        #     direkt im Worktree editiert hat).
        validation = self._validate_changes(worktree, proposal, gen_id)
        if not validation.success:
            outcome = GenerationOutcome(
                idx=gen_idx,
                kept=False,
                reason=validation.error_class or "mutation_failed",
                cost_usd=proposal.cost_usd,
                error="guardrail" if validation.capability_violation else None,
            )
            self._finish_generation(gen_id, outcome)
            return outcome

        # --- Phase 3: Preflight --------------------------------------
        preflight_err = self._preflight(worktree, validation.files_changed, gen_id)
        if preflight_err is not None:
            self.worktrees.revert(worktree, to_commit=self._current_base_commit)
            outcome = GenerationOutcome(
                idx=gen_idx,
                kept=False,
                reason="preflight_failed",
                cost_usd=proposal.cost_usd,
            )
            self._finish_generation(gen_id, outcome)
            return outcome

        # --- Phase 4: Eval --------------------------------------------
        eval_result = self._eval(worktree, gen_id)
        measurements = dict(eval_result.measurements)

        # --- Phase 4b: Judge (opt-in, Spec v0.5) ----------------------
        # Verifiziert den Diff gegen die Akzeptanzkriterien. Der Score
        # wird als llm_judge_score in die Measurements gemerged, BEVOR die
        # Gates ausgewertet werden — ein llm_judge_score-Gate macht ihn
        # damit in der unveränderten Decide-Logik bindend (rot→grün-
        # Revival, sobald der Judge das Feature bestätigt).
        judge_outcome: JudgeOutcome | None = None
        if self._judge is not None:
            judge_outcome = self._run_judge(worktree, gen_id, proposal.diff)
            measurements.update(judge_outcome.measurement)

        gates_passed, gate_results = evaluate_gates(
            measurements=measurements,
            spec=self.config.spec,
            baseline=self._gate_baseline,
        )
        composite = (
            compute_composite(
                measurements=measurements,
                spec=self.config.spec,
                baseline=self._score_baseline,
            )
            if gates_passed
            else None
        )

        # EvalFinished mit allem (Gates inkl. judge, falls aktiviert)
        eval_payload = EvalFinishedPayload(
            eval_mode="quick",
            suite_id=self.config.eval_suite,
            gates_passed=gates_passed,
            gates=gate_results,
            scores={
                k: v
                for k, v in measurements.items()
                if any(s.kind == k for s in self.config.spec.scores)
            },
            composite_value=composite,
            diagnostics={
                k: v
                for k, v in measurements.items()
                if any(d.kind == k for d in self.config.spec.diagnostics)
            },
        )
        eval_artifacts = self._eval_artifacts(eval_result)
        self._emit(
            EventKind.EVAL_FINISHED,
            eval_payload,
            generation_id=gen_id,
            artifacts=eval_artifacts,
            duration_ms=eval_result.duration_ms,
            success=gates_passed,
        )

        # --- Phase 5: Decide ------------------------------------------
        kept = False
        reason = "gate_failure"
        score_delta: float | None = None
        new_gate_results = {g.kind: g.passed for g in gate_results}
        # gates_passed ist hier per Logik True — aber wir reichen die
        # detaillierte Map an keep_or_discard, damit Modus 3 (Trade-off)
        # vorher-grüne-jetzt-rote-Gates erkennen kann.
        if gates_passed:
            kept, reason = keep_or_discard(
                new_composite=composite,
                baseline_composite=self._baseline_composite,
                tolerance=self.config.tolerance,
                baseline_gates_passed=self._baseline_gates_passed,
                baseline_gate_results=self._baseline_gate_results,
                new_gate_results=new_gate_results,
            )
            if (
                composite is not None
                and self._baseline_composite is not None
            ):
                score_delta = composite - self._baseline_composite
            elif composite is not None:
                score_delta = composite

        self._emit(
            EventKind.DECISION_MADE,
            DecisionMadePayload(
                kept=kept,
                reason=reason,  # type: ignore[arg-type]
                score_delta=score_delta,
                tolerance=self.config.tolerance,
            ),
            generation_id=gen_id,
        )

        if kept:
            commit_msg = self._format_commit_message(gen_idx, score_delta)
            # Nur Surface-Files committen — Subagent-Files (.claude/agents/*.md)
            # sind transient und gehören nicht in den PR.
            prev_base = self._current_base_commit or worktree.base_commit
            new_sha = self.worktrees.commit(
                worktree,
                commit_msg,
                paths=validation.files_changed or None,
            )
            # Inter-Generation-Memory: Diff dieser KEPT-Generation für
            # nächste Generation registrieren.
            kept_diff = self._capture_kept_diff(worktree, prev_base, new_sha)
            self._run_context.record_keep(generation_idx=gen_idx, diff=kept_diff)
            # Revert-Anker aktualisieren, damit DISCARD in einer späteren
            # Generation NICHT diesen KEPT-Commit wegblastet.
            self._current_base_commit = new_sha
            # Baseline für nächste Generation aktualisieren — mit den
            # gemergten Measurements (inkl. judge), damit die nächste
            # Generation den Judge-Score korrekt als grüne Baseline kennt.
            self._update_baselines_from_eval(measurements, composite, gate_results)
        else:
            self.worktrees.revert(worktree, to_commit=self._current_base_commit)

        gen_cost = proposal.cost_usd + (
            judge_outcome.cost_usd if judge_outcome is not None else Decimal("0")
        )
        outcome = GenerationOutcome(
            idx=gen_idx,
            kept=kept,
            reason=reason,
            composite=composite,
            score_delta=score_delta,
            cost_usd=gen_cost,
            # Self-Termination wird am Generation-Outcome durchgereicht,
            # damit der Run-Loop danach abbricht — das Generation-Outcome
            # selbst (kept/reason) reflektiert aber das echte Eval-Resultat.
            error="self_terminated" if is_self_terminated else None,
        )
        self._finish_generation(gen_id, outcome)
        return outcome

    # --- Phasen-Helpers --------------------------------------------------

    def _propose(
        self,
        worktree: Worktree,
        gen_id: str,
        gen_idx: int,
    ) -> ProposalResult | None:
        # Project memory (cross-run) + inter-generation memory (within-run).
        from forge_execute._project_memory import (
            build_project_memory,
            render_project_memory_addendum,
        )

        memory_md = build_project_memory(
            forge_dir=self.config.repo_root / ".forge",
            store=self.store,
            blobs=self.blobs,
            project=self.config.project,
            exclude_run_id=self.run_id,
        )
        memory_addendum = render_project_memory_addendum(memory_md)
        run_addendum = self._run_context.render_prompt_addendum()
        effective_prompt = (
            self.config.initial_prompt + memory_addendum + run_addendum
        )

        prompt_hash = self.blobs.put_text(effective_prompt)
        proposal_artifacts: dict[str, str] = {"prompt": prompt_hash}
        context_keys = ["prompt"]
        if memory_md.strip():
            proposal_artifacts["project_memory"] = self.blobs.put_text(memory_md)
            context_keys.append("project_memory")

        self._emit(
            EventKind.PROPOSAL_REQUESTED,
            ProposalRequestedPayload(
                prompt_template_id=self.config.prompt_template_id,
                context_artifact_keys=context_keys,
                max_turns=self.config.max_turns_per_proposal,
                requested_model=self.config.model,
            ),
            generation_id=gen_id,
            artifacts=proposal_artifacts,
        )

        try:
            result = self.agent.propose(
                worktree=worktree.path,
                prompt=effective_prompt,
                max_turns=self.config.max_turns_per_proposal,
                budget_usd=self.config.spec.cost_caps.per_generation_usd,
                model=self.config.model,
                allowed_tools=self.capabilities.allowed_tools_string(),
            )
            # Self-Termination-Signal (Spec v0.3 Teil 6.8): wenn der Agent
            # `forge: nothing more to do` im result-text gesendet hat, ist
            # der Run vollständig.
            from forge_execute._run_context import proposal_signals_done

            done_signal = proposal_signals_done(
                str(result.raw_response.get("result") or "")
            )
            if done_signal:
                # Markiere im error-Feld; der Caller bricht den Run-Loop ab.
                result = result.__class__(
                    **{
                        **result.__dict__,
                        "error": "self_terminated",
                    }
                )
        except CodingAgentTimeout as exc:
            self._emit(
                EventKind.PROPOSAL_RECEIVED,
                ProposalReceivedPayload(stop_reason="timeout"),
                generation_id=gen_id,
                error_class="CodingAgentTimeout",
                error_msg=str(exc),
                success=False,
            )
            return None
        except CodingAgentError as exc:
            self._emit(
                EventKind.PROPOSAL_RECEIVED,
                ProposalReceivedPayload(stop_reason="error"),
                generation_id=gen_id,
                error_class="CodingAgentError",
                error_msg=str(exc),
                success=False,
            )
            return None

        # Diff als Artefakt
        artifacts: dict[str, str] = {}
        if result.diff:
            artifacts["diff"] = self.blobs.put_text(result.diff)

        # Cost akkumulieren
        self._total_cost += result.cost_usd

        # Plan-Persistierung (Spec v0.3 Teil 6.1) — vor ProposalReceived emittieren,
        # damit die zeitliche Reihenfolge im Replay stimmt: PlanProposed kommt
        # logisch BEFORE der eigentlichen Code-Mutation, auch wenn beide aus
        # demselben claude-Aufruf stammen.
        if result.plan_md is not None:
            self._emit_plan_proposed(
                plan_md=result.plan_md,
                gen_id=gen_id,
                architect_turns=result.turns_used,
                agents_invoked=result.agents_invoked,
            )

        self._emit(
            EventKind.PROPOSAL_RECEIVED,
            ProposalReceivedPayload(
                stop_reason=_normalize_stop_reason(result.stop_reason),
                turns_used=result.turns_used,
                files_touched=[],
            ),
            generation_id=gen_id,
            artifacts=artifacts,
            tokens_in=result.tokens_in,
            tokens_out=result.tokens_out,
            cost_usd=result.cost_usd,
            model=result.model,
            model_version=result.model_version,
            duration_ms=result.duration_ms,
            success=True,
        )
        return result

    def _emit_plan_proposed(
        self,
        *,
        plan_md: str,
        gen_id: str,
        architect_turns: int,
        agents_invoked: list[str] | None = None,
    ) -> None:
        """Persistiert den Plan im Blob-Store und emittiert PlanProposed.

        `architect_turns` ist heute der Master-Total — wir haben kein
        feinkörniges Subagent-Turn-Tracking. Spec v0.3 erlaubt das (das
        Feld ist informativ, nicht semantisch).

        `agents_used` spiegelt, welche Rollen der Master laut Selbstauskunft
        TATSÄCHLICH gerufen hat (`agents_invoked`), und fällt nur dann auf das
        konfigurierte Roster zurück, wenn der Master nichts meldete. Das Feld
        bedeutet semantisch „mitgewirkt" — die Realität ist treuer als die
        bloße Config.
        """
        from forge_core.events.kinds.plan import PlanProposedPayload, PlanSubtask

        from forge_execute._plan_parser import parse_plan

        plan_hash = self.blobs.put_text(plan_md)
        parsed = parse_plan(plan_md)
        self._emit(
            EventKind.PLAN_PROPOSED,
            PlanProposedPayload(
                architect_turns=architect_turns,
                subtask_count=parsed.subtask_count,
                risk_level=parsed.risk_level,
                out_of_scope=parsed.out_of_scope,
                insufficient_context=parsed.insufficient_context,
                subtasks=[
                    PlanSubtask(index=s.index, title=s.title, status=s.status)
                    for s in parsed.subtasks
                ],
                agents_used=agents_invoked or list(self.config.agents),
            ),
            generation_id=gen_id,
            artifacts={"plan": plan_hash},
        )

    def _validate_changes(
        self,
        worktree: Worktree,
        proposal: ProposalResult,
        gen_id: str,
    ) -> MutationResult:
        """Validiert die schon im Worktree liegenden Änderungen.

        Anders als bei `mutator.apply()` (das den Diff anwendet) prüfen wir
        hier nachträglich: Capability-Pfade + Syntax. Bei Failure: revert.
        """
        from forge_execute.mutators.code import (
            count_diff_lines,
            extract_changed_paths,
        )

        diff = proposal.diff
        target_paths = self.worktrees.changed_files(worktree) or extract_changed_paths(diff)

        # Capability-Check
        for path in target_paths:
            check = self.capabilities.check_edit(path)
            if check.denied:
                self._emit(
                    EventKind.GUARDRAIL_VIOLATION,
                    check.to_violation(f"edit {path}"),
                    generation_id=gen_id,
                    success=False,
                )
                self.worktrees.revert(worktree, to_commit=self._current_base_commit)
                return MutationResult(
                    success=False,
                    error_class="CapabilityDenied",
                    error_msg=check.detail,
                    capability_violation=check,
                )

        # Whitespace + Syntax — wir reverten bei Failure.
        ws = self.mutator._whitespace_check(worktree)
        if ws is not None:
            self.worktrees.revert(worktree, to_commit=self._current_base_commit)
            self._emit(
                EventKind.GUARDRAIL_VIOLATION,
                GuardrailViolationPayload(
                    guardrail_id="surface_violation",  # bestes Match v1
                    attempted_action="introduce whitespace error",
                    detail=ws,
                ),
                generation_id=gen_id,
                success=False,
            )
            return MutationResult(
                success=False, error_class="WhitespaceError", error_msg=ws
            )

        syntax = self.mutator._syntax_check(worktree, target_paths)
        if syntax is not None:
            self.worktrees.revert(worktree, to_commit=self._current_base_commit)
            return MutationResult(
                success=False, error_class="SyntaxError", error_msg=syntax
            )

        added, removed = count_diff_lines(diff)
        self._emit(
            EventKind.MUTATION_APPLIED,
            MutationAppliedPayload(
                mutator="code",
                files_changed=target_paths,
                lines_added=added,
                lines_removed=removed,
            ),
            generation_id=gen_id,
        )
        return MutationResult(
            success=True,
            files_changed=target_paths,
            lines_added=added,
            lines_removed=removed,
        )

    def _preflight(
        self,
        worktree: Worktree,
        files_changed: list[str],
        gen_id: str,
    ) -> str | None:
        """Führt die `surfaces.<name>.guardrails`-Checks der betroffenen
        Surfaces aus (Spec Teil 6.3).

        Liefert `None` bei Erfolg, sonst die fehlgeschlagene Befehlszeile.
        Budget pro Check ist hart 30s.
        """
        # Sammle die Guardrails der betroffenen Surfaces (dedupliziert)
        relevant_guardrails: list[tuple[str, str]] = []  # (surface, command)
        seen_cmds: set[str] = set()
        for path in files_changed:
            match = self.config.spec.surface_for_path(path)
            if match is None:
                continue
            surface_name, surface = match
            for cmd in surface.guardrails:
                if cmd in seen_cmds:
                    continue
                seen_cmds.add(cmd)
                relevant_guardrails.append((surface_name, cmd))

        for idx, (surface_name, cmd) in enumerate(relevant_guardrails):
            preflight_id = f"{surface_name}#{idx}"
            # Kommando-Substitution: `{path}` mit konkreten Pfaden ersetzen
            for path in files_changed:
                subst = cmd.replace("{path}", path) if "{path}" in cmd else cmd
                ok = _quick_run(subst, cwd=worktree.path, timeout=30)
                if ok is None or ok != 0:
                    self._emit(
                        EventKind.PREFLIGHT_FAILED,
                        PreflightFailedPayload(
                            preflight_id=preflight_id,
                            surface=surface_name,
                            command=subst,
                            exit_code=ok if ok is not None else 124,
                        ),
                        generation_id=gen_id,
                        success=False,
                    )
                    return subst
                if "{path}" not in cmd:
                    break  # Befehl nicht pfadabhängig — einmal reicht
        return None

    def _eval(self, worktree: Worktree, gen_id: str) -> EvalRunResult:
        suite = self.config.spec.eval_suites.get(self.config.eval_suite)
        if suite is None:
            # Fallback: künstliche grüne Suite
            return EvalRunResult(
                suite_id=self.config.eval_suite,
                exit_code=0,
                duration_ms=0,
                timeout=False,
                measurements={},
            )
        self._emit(
            EventKind.EVAL_STARTED,
            EvalStartedPayload(
                eval_mode="quick",
                suite_id=self.config.eval_suite,
                budget_s=suite.budget_s,
            ),
            generation_id=gen_id,
        )
        return self.evaluator.run(
            suite_id=self.config.eval_suite,
            suite=suite,
            cwd=worktree.path,
        )

    def _eval_baseline(self, worktree: Worktree) -> EvalRunResult:
        """Initial-Eval VOR der ersten Generation, um eine Baseline zu kennen."""
        suite = self.config.spec.eval_suites.get(self.config.eval_suite)
        if suite is None:
            return EvalRunResult(
                suite_id=self.config.eval_suite,
                exit_code=0,
                duration_ms=0,
                timeout=False,
                measurements={},
            )
        return self.evaluator.run(
            suite_id=self.config.eval_suite,
            suite=suite,
            cwd=worktree.path,
        )

    def _run_judge(
        self,
        worktree: Worktree,
        gen_id: str,
        diff: str,
    ) -> JudgeOutcome:
        """Führt die Judge-Phase aus und emittiert das Eventpaar.

        Eigenes ``EVAL_STARTED``/``EVAL_FINISHED``-Paar mit
        ``eval_mode="judge"`` (Mantra 2: jeder Schritt ein Event). Das
        autoritative Gate-Resultat steht im nachfolgenden
        ``EVAL_FINISHED(eval_mode="quick")``; dieses Event hier ist die
        Judge-Ökonomie + Begründung für den Replay. Fail-closed liegt
        im :class:`JudgeEvaluator`.
        """
        assert self._judge is not None
        judge_cfg = self.config.spec.judge
        self._emit(
            EventKind.EVAL_STARTED,
            EvalStartedPayload(
                eval_mode="judge",
                suite_id="judge",
                budget_s=judge_cfg.budget_s,
            ),
            generation_id=gen_id,
        )

        acceptance = self.config.acceptance_criteria or self.config.initial_prompt
        outcome = self._judge.run(
            worktree=worktree.path,
            acceptance_criteria=acceptance,
            diff=diff,
            max_turns=judge_cfg.max_turns,
            budget_usd=self.config.spec.cost_caps.per_generation_usd,
            model=judge_cfg.model or self.config.model,
        )
        self._total_cost += outcome.cost_usd

        artifacts: dict[str, str] = {}
        if outcome.reasoning:
            artifacts["judge_reasoning"] = self.blobs.put_text(outcome.reasoning)

        self._emit(
            EventKind.EVAL_FINISHED,
            EvalFinishedPayload(
                eval_mode="judge",
                suite_id="judge",
                gates_passed=outcome.verdict == "pass",
                gates=[],
                scores={},
                composite_value=None,
                diagnostics=dict(outcome.measurement),
            ),
            generation_id=gen_id,
            artifacts=artifacts,
            cost_usd=outcome.cost_usd,
            model=outcome.model,
            duration_ms=outcome.duration_ms or None,
            success=outcome.error is None,
            error_class="JudgeError" if outcome.error else None,
            error_msg=outcome.error,
        )
        return outcome

    # --- Bookkeeping -----------------------------------------------------

    def _update_baselines(self, eval_result: EvalRunResult) -> None:
        self._gate_baseline = GateBaseline(values=dict(eval_result.measurements))
        self._score_baseline = ScoreBaseline(values=dict(eval_result.measurements))
        gates_passed, gate_results = evaluate_gates(
            measurements=eval_result.measurements,
            spec=self.config.spec,
            baseline=None,
        )
        self._baseline_gates_passed = gates_passed
        self._baseline_gate_results = {g.kind: g.passed for g in gate_results}
        self._baseline_composite = (
            compute_composite(
                measurements=eval_result.measurements,
                spec=self.config.spec,
                baseline=None,
            )
            if gates_passed
            else None
        )

    def _update_baselines_from_eval(
        self,
        measurements: dict[str, float],
        composite: float | None,
        gate_results: list | None = None,
    ) -> None:
        self._gate_baseline = GateBaseline(values=dict(measurements))
        self._score_baseline = ScoreBaseline(values=dict(measurements))
        # Wenn wir KEEP gemacht haben, sind die Gates per Definition grün.
        self._baseline_gates_passed = True
        if gate_results is not None:
            self._baseline_gate_results = {g.kind: g.passed for g in gate_results}
        if composite is not None:
            self._baseline_composite = composite

    def _check_run_cost_cap(self) -> Decimal | None:
        cap = self.config.spec.cost_caps.per_run_usd
        if self._total_cost > cap:
            self._emit(
                EventKind.COST_CAP_HIT,
                CostCapHitPayload(level="run", cap_usd=cap, actual_usd=self._total_cost),
            )
            return cap
        return None

    def _eval_artifacts(self, result: EvalRunResult) -> dict[str, str]:
        out: dict[str, str] = {}
        if result.stdout:
            out["eval_stdout"] = self.blobs.put_text(result.stdout)
        if result.stderr:
            out["eval_stderr"] = self.blobs.put_text(result.stderr)
        if result.tool_versions:
            tools_text = "\n".join(f"{k}: {v}" for k, v in result.tool_versions.items())
            out["tool_versions"] = self.blobs.put_text(tools_text)
        return out

    def _capture_kept_diff(
        self, worktree: Worktree, from_commit: str, to_commit: str
    ) -> str:
        """Holt `git diff <from_commit>..<to_commit>` aus dem Worktree."""
        result = subprocess.run(
            ["git", "diff", from_commit, to_commit],
            cwd=str(worktree.path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return result.stdout if result.returncode == 0 else ""

    def _format_commit_message(
        self, gen_idx: int, score_delta: float | None
    ) -> str:
        focus_part = self.config.focus or "auto"
        delta_part = (
            f"composite {'+' if score_delta is not None and score_delta >= 0 else ''}{score_delta:.3f}"
            if score_delta is not None
            else "composite n/a"
        )
        return f"forge: {focus_part} | gen {gen_idx} | {delta_part}"

    def _compute_config_hash(self) -> str:
        """Hash über die für diesen Run relevante Spec-Subset.

        Ändert sich, wenn der Operator Surfaces/Gates/Scores anpasst — Trigger
        für Loop 3, gleiche Konfiguration nicht mehrmals zu probieren.
        """
        import hashlib
        import json

        subset = {
            "surfaces": {
                k: v.model_dump() for k, v in self.config.spec.surfaces.items()
            },
            "gates": [g.model_dump() for g in self.config.spec.gates],
            "scores": [s.model_dump() for s in self.config.spec.scores],
            "eval_suite": self.config.eval_suite,
            "focus": self.config.focus,
        }
        canonical = json.dumps(subset, sort_keys=True, default=str).encode("utf-8")
        return "sha256:" + hashlib.sha256(canonical).hexdigest()

    # --- Event-Emission -------------------------------------------------

    def _emit(
        self,
        kind: EventKind,
        payload,
        *,
        generation_id: str | None = None,
        artifacts: dict[str, str] | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: Decimal | None = None,
        model: str | None = None,
        model_version: str | None = None,
        duration_ms: int | None = None,
        success: bool | None = None,
        error_class: str | None = None,
        error_msg: str | None = None,
    ) -> None:
        evt = build_event(
            kind=kind,
            run_id=self.run_id,
            generation_id=generation_id,
            payload=payload,
            artifacts=artifacts or {},
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd if cost_usd is not None else Decimal("0"),
            model=model,
            model_version=model_version,
            duration_ms=duration_ms,
            success=success,
            error_class=error_class,
            error_msg=error_msg,
            **self._common,
        )
        self.store.append(evt)

    def _finish_generation(
        self,
        gen_id: str,
        outcome: GenerationOutcome,
    ) -> None:
        self._emit(
            EventKind.GENERATION_FINISHED,
            GenerationFinishedPayload(
                generation_idx=outcome.idx,
                decision="keep" if outcome.kept else "discard",
                new_score=outcome.composite,
                score_delta=outcome.score_delta,
                reason=outcome.reason,
            ),
            generation_id=gen_id,
        )


# --- Module-level helpers ----------------------------------------------


_ALLOWED_STOP_REASONS = {
    "end_turn",
    "max_tokens",
    "max_turns",
    "stop_sequence",
    "tool_use",
    "error",
    "timeout",
    "unknown",
}


def _normalize_stop_reason(stop: str) -> str:
    return stop if stop in _ALLOWED_STOP_REASONS else "unknown"


def _quick_run(cmd: str, *, cwd: Path, timeout: int) -> int | None:
    """Führt einen Preflight-Befehl aus und liefert den Exit-Code (oder None
    bei Timeout)."""
    from forge_execute._venv import venv_aware_env

    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            env=venv_aware_env(cwd),
        )
        return result.returncode
    except subprocess.TimeoutExpired:
        return None
