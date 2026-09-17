"""Bounded autonomous refinement loop around ``ScenarioRunner``.

Each iteration produces exactly one new simulated conversation. If that run
needs a change, the Refiner plans one and the deterministic Applier makes one
validated update; the next iteration supplies the verification evidence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

from app.config import ELEVENLABS_REFINEMENT_BRANCH_ID
from app.guardrails import GuardrailExceeded, GuardrailLimits, RunGuardrails
from app.refinement_applier import AppliedRefinement, RefinementApplier, RefinementApplyError
from app.refiner import RefinementPlan, RefinementPlanner, RefinementPlannerError
from app.run_logging import append_run_event, read_run_events
from app.simulation_runner import ScenarioRunner, SimulationResult, SimulationRunnerError


class RefinementRunnerError(RuntimeError):
    """Raised when an autonomous refinement job cannot proceed safely."""


@dataclass(frozen=True)
class RefinementIteration:
    iteration: int
    simulation: SimulationResult
    plan: RefinementPlan | None
    applied: AppliedRefinement | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "iteration": self.iteration,
            "simulation": {
                "run_id": self.simulation.run_id,
                "scenario_id": self.simulation.scenario_id,
                "outcome": self.simulation.outcome,
                "conversation_id": self.simulation.conversation_id,
                "transcript_path": self.simulation.transcript_path,
                "evaluation": self.simulation.evaluation.as_dict() if self.simulation.evaluation else None,
                "llm_evaluation": self.simulation.llm_evaluation.as_dict() if self.simulation.llm_evaluation else None,
            },
            "plan": self.plan.as_dict() if self.plan else None,
            "applied": self.applied.as_dict() if self.applied else None,
        }


@dataclass(frozen=True)
class RefinementRunResult:
    outcome: str
    iterations: tuple[RefinementIteration, ...]
    guardrails: dict[str, int]
    applied_changes: int
    dry_run: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "iterations": [iteration.as_dict() for iteration in self.iterations],
            "guardrails": self.guardrails,
            "applied_changes": self.applied_changes,
            "dry_run": self.dry_run,
        }


def _provider_calls_from_run(run_id: str) -> int:
    """Count completed provider work after a scenario, for the job budget."""
    events = read_run_events(run_id)
    return sum(
        1
        for event in events
        if event.get("event_type") in {"customer_simulator_completed", "llm_evaluation_completed"}
        or (event.get("event_type") == "message" and event.get("role") == "customer")
    )


def _tool_calls_from_run(run_id: str) -> int:
    return sum(1 for event in read_run_events(run_id) if event.get("event_type") == "tool_request_finished")


class RefinementRunner:
    """Run bounded, branch-scoped simulate → evaluate → plan → apply loops."""

    def __init__(
        self,
        *,
        scenario_runner: ScenarioRunner | Any | None = None,
        planner: RefinementPlanner | Any | None = None,
        applier: RefinementApplier | Any | None = None,
        limits: GuardrailLimits | None = None,
        refinement_branch_id: str | None = None,
    ) -> None:
        self.limits = limits or GuardrailLimits()
        self.guardrails = RunGuardrails(self.limits)
        self.refinement_branch_id = refinement_branch_id or ELEVENLABS_REFINEMENT_BRANCH_ID
        self._scenario_runner = scenario_runner or ScenarioRunner()
        self._planner = planner or RefinementPlanner(refinement_branch_id=self.refinement_branch_id)
        self._applier = applier or RefinementApplier(refinement_branch_id=self.refinement_branch_id)

    def run(self, scenario_id: str, *, apply_changes: bool = False) -> RefinementRunResult:
        """Run one scenario repeatedly only when the previous iteration changed it."""
        if not self.refinement_branch_id:
            raise RefinementRunnerError("A dedicated ElevenLabs refinement branch is required for autonomous refinement.")
        current_scenario_id = scenario_id
        iterations: list[RefinementIteration] = []
        applied_changes = 0
        try:
            for iteration_number in range(1, self.limits.max_iterations + 1):
                self.guardrails.start_scenario()
                simulation = self._scenario_runner.run(
                    current_scenario_id,
                    run_llm_review=True,
                    agent_branch_id=self.refinement_branch_id,
                    max_customer_turns=self.limits.max_turns_per_conversation,
                )
                provider_calls = _provider_calls_from_run(simulation.run_id)
                for _ in range(provider_calls):
                    self.guardrails.register_agent_call()
                self.guardrails.validate_conversation(
                    turns=simulation.customer_turns,
                    tool_calls=_tool_calls_from_run(simulation.run_id),
                )
                if not simulation.llm_evaluation:
                    append_run_event(simulation.run_id, "refinement_stopped", reason="llm_evaluation_unavailable")
                    iterations.append(RefinementIteration(iteration_number, simulation, None, None))
                    return RefinementRunResult(
                        "llm_evaluation_unavailable",
                        tuple(iterations),
                        self.limits.as_dict(),
                        applied_changes,
                        not apply_changes,
                    )

                self.guardrails.register_agent_call()  # the paid Refiner request about to occur
                plan = self._planner.plan(simulation.run_id, simulation.evaluation)
                applied = self._applier.apply(plan, apply=apply_changes)
                iterations.append(RefinementIteration(iteration_number, simulation, plan, applied))
                append_run_event(
                    simulation.run_id,
                    "refinement_iteration_completed",
                    iteration=iteration_number,
                    plan_status=plan.status,
                    apply_status=applied.status,
                    dry_run=not apply_changes,
                )
                if plan.status == "no_change":
                    return RefinementRunResult(
                        "passed",
                        tuple(iterations),
                        self.limits.as_dict(),
                        applied_changes,
                        not apply_changes,
                    )
                if not apply_changes:
                    return RefinementRunResult(
                        "plan_validated_not_applied",
                        tuple(iterations),
                        self.limits.as_dict(),
                        applied_changes,
                        True,
                    )
                applied_changes += 1
                current_scenario_id = plan.verification_scenario_id
            return RefinementRunResult(
                "iteration_limit_reached",
                tuple(iterations),
                self.limits.as_dict(),
                applied_changes,
                not apply_changes,
            )
        except (GuardrailExceeded, SimulationRunnerError, RefinementPlannerError, RefinementApplyError) as error:
            raise RefinementRunnerError("Autonomous refinement stopped safely.") from error
