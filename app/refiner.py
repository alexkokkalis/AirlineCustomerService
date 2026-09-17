"""Safe, structured planning for the autonomous refinement loop.

The refiner is intentionally a planner in this first stage: it examines a
completed run and its evaluations, then produces one small validated proposal.
It has no filesystem, shell, database, webhook, or ElevenLabs mutation tool.
An application-controlled applier will be added only after this output and its
verification path are exercised on the dedicated refinement branch.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI

from app.config import ELEVENLABS_REFINEMENT_BRANCH_ID, OPENAI_API_KEY, OPENAI_REFINER_MODEL
from app.customer_simulator import visible_transcript
from app.elevenlabs_agent_config import AgentPromptSnapshot, ElevenLabsAgentConfigClient, ElevenLabsAgentConfigError
from app.evaluation import EvaluationResult, evaluate_run
from app.llm_evaluator import _audit_summary
from app.refinement_context import REFINEMENT_SYSTEM_CONTEXT
from app.run_logging import append_run_event, read_run_events, validate_run_id
from app.scenarios import get_scenario


ROOT = Path(__file__).resolve().parents[1]
REFINEMENT_DIR = ROOT / "logs" / "refinements"

RefinementTarget = Literal["erling_prompt", "code", "none"]
RefinementOperation = Literal["append", "replace", "none"]

# The future applier may only operate on this deliberately small set of source
# files. The planner receives excerpts from these files only for code issues.
ALLOWED_CODE_FILES = frozenset(
    {
        "app/customer_simulator.py",
        "app/simulation_runner.py",
        "app/ionian_api.py",
        "app/evaluation.py",
        "app/llm_evaluator.py",
        "app/scenario_fixtures.py",
        "app/scenarios.py",
    }
)


class RefinementPlannerError(RuntimeError):
    """Raised when a refinement plan cannot be generated or validated."""


@dataclass(frozen=True)
class RefinementPlan:
    """One dry-run, application-validated proposed change."""

    run_id: str
    scenario_id: str
    model: str
    source_decision: str
    status: Literal["refinement_needed", "no_change"]
    target: RefinementTarget
    operation: RefinementOperation
    target_file: str
    expected_current_text: str
    replacement_text: str
    rationale: str
    expected_effect: str
    verification_scenario_id: str
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    plan_path: str
    dry_run: bool = True

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


REFINEMENT_PLAN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["refinement_needed", "no_change"]},
        "target": {"type": "string", "enum": ["erling_prompt", "code", "none"]},
        "operation": {"type": "string", "enum": ["append", "replace", "none"]},
        "target_file": {"type": "string"},
        "expected_current_text": {"type": "string"},
        "replacement_text": {"type": "string"},
        "rationale": {"type": "string"},
        "expected_effect": {"type": "string"},
        "verification_scenario_id": {"type": "string"},
    },
    "required": [
        "status",
        "target",
        "operation",
        "target_file",
        "expected_current_text",
        "replacement_text",
        "rationale",
        "expected_effect",
        "verification_scenario_id",
    ],
}


REFINER_INSTRUCTIONS = f"""You are the planning component of a controlled autonomous refinement loop for an airline-agent assessment.

{REFINEMENT_SYSTEM_CONTEXT}

You receive a completed scenario, its transcript, deterministic report, and an independent LLM review. Produce exactly one small, evidence-backed plan, not a discussion.

Plan constraints:
- Treat the LLM review decision as a useful diagnosis, but independently check it against the deterministic report and transcript.
- If the evaluation passed, return status `no_change`, target `none`, operation `none`, and empty strings for all change fields except rationale, expected_effect, and verification_scenario_id.
- Otherwise choose exactly one primary target: `erling_prompt` or `code`. Do not use `none` for a failing run.
- For `erling_prompt`, use operation `replace`, target_file `elevenlabs_system_prompt`, and replace one exact, concise section from `current_erling_system_prompt`. Do not rewrite the whole prompt or make unsupported policy claims.
- For `code`, use operation `replace`, choose one target_file from the supplied allowlist, and provide an exact unique expected_current_text plus its replacement_text. Never propose a patch outside the supplied safe excerpts.
- Do not propose more than one change, credentials, database changes, API key changes, dependency installs, shell commands, git commands, or direct external mutations.
- verification_scenario_id must be the supplied scenario id unless a different supplied scenario directly tests the same defect.
- Keep all change text minimal and directly tied to evidence.
"""


def refinement_plan_path(run_id: str) -> Path:
    """Return a safe path for the derived plan associated with one run."""
    valid_run_id = validate_run_id(run_id)
    if not valid_run_id:
        raise ValueError("run_id must contain only letters, numbers, underscores, or hyphens.")
    return REFINEMENT_DIR / f"{valid_run_id}.plan.json"


def _run_scenario_id(events: list[dict[str, Any]]) -> str:
    started = next((event for event in events if event.get("event_type") == "run_started"), None)
    scenario_id = started.get("scenario_id") if started else None
    if not isinstance(scenario_id, str):
        raise RefinementPlannerError("A scenario_id is required before refinement can be planned.")
    return scenario_id


def _review_payload(run_id: str) -> dict[str, Any]:
    path = ROOT / "logs" / "evaluations" / f"{run_id}.llm.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RefinementPlannerError("Run the LLM transcript evaluation before requesting a refinement plan.") from error
    if not isinstance(payload, dict) or not isinstance(payload.get("decision"), dict):
        raise RefinementPlannerError("The LLM evaluation report has no usable routing decision.")
    return payload


def _code_excerpts(review: dict[str, Any]) -> dict[str, str]:
    """Expose only narrow, non-secret source context relevant to a code diagnosis."""
    if review.get("decision", {}).get("decision") not in {"code_fix_needed", "mixed_refinement_needed"}:
        return {}
    text = json.dumps(review.get("failures", [])).lower()
    candidates: set[str] = set()
    if any(term in text for term in ("simulator", "customer", "max-turn", "terminal")):
        candidates.add("app/customer_simulator.py")
    if any(term in text for term in ("runner", "orchestrat", "transcript", "loop")):
        candidates.add("app/simulation_runner.py")
    if any(term in text for term in ("webhook", "backend", "api", "tool request")):
        candidates.add("app/ionian_api.py")
    if any(term in text for term in ("deterministic", "evaluator", "evaluation")):
        candidates.update({"app/evaluation.py", "app/llm_evaluator.py"})
    if not candidates:
        candidates.add("app/simulation_runner.py")

    excerpts: dict[str, str] = {}
    for relative_path in sorted(candidates & ALLOWED_CODE_FILES):
        try:
            contents = (ROOT / relative_path).read_text()
        except OSError:
            continue
        excerpts[relative_path] = contents[:12_000]
    return excerpts


def _planning_input(
    *,
    scenario_id: str,
    events: list[dict[str, Any]],
    deterministic: EvaluationResult,
    review: dict[str, Any],
    prompt_snapshot: AgentPromptSnapshot | None,
) -> dict[str, Any]:
    scenario = get_scenario(scenario_id)
    return {
        "scenario": {
            "id": scenario.id,
            "title": scenario.title,
            "customer_goal": scenario.customer_goal,
            "expected_tools": scenario.expected_tools,
            "pass_criteria": scenario.pass_criteria,
        },
        "visible_transcript": visible_transcript(events),
        "tool_audit": _audit_summary(events),
        "deterministic_evaluation": deterministic.as_dict(),
        "llm_evaluation": review,
        "code_change_allowlist": sorted(ALLOWED_CODE_FILES),
        "safe_code_excerpts": _code_excerpts(review),
        "current_erling_system_prompt": prompt_snapshot.system_prompt if prompt_snapshot else None,
        "erling_prompt_snapshot": (
            {
                "agent_id": prompt_snapshot.agent_id,
                "branch_id": prompt_snapshot.branch_id,
                "version_id": prompt_snapshot.version_id,
            }
            if prompt_snapshot
            else None
        ),
    }


def _validate_plan_payload(
    payload: dict[str, Any], *, scenario_id: str, review: dict[str, Any], prompt_snapshot: AgentPromptSnapshot | None
) -> None:
    status = payload["status"]
    target = payload["target"]
    operation = payload["operation"]
    source_decision = review["decision"].get("decision")
    if payload["verification_scenario_id"] != scenario_id:
        raise RefinementPlannerError("The plan must verify against the scenario that produced the evidence.")
    if status == "no_change":
        if source_decision != "passed" or (target, operation) != ("none", "none"):
            raise RefinementPlannerError("Only a passing evaluation may produce a no-change plan.")
        return
    if source_decision == "passed":
        raise RefinementPlannerError("A passing evaluation cannot produce a refinement plan.")
    if target == "erling_prompt":
        if operation != "replace" or payload["target_file"] != "elevenlabs_system_prompt":
            raise RefinementPlannerError("Prompt plans must replace a section in the ElevenLabs system prompt.")
        expected = payload["expected_current_text"]
        if not prompt_snapshot:
            raise RefinementPlannerError("Prompt plans require a retrieved ElevenLabs refinement-branch prompt.")
        if not expected.strip() or not payload["replacement_text"].strip():
            raise RefinementPlannerError("Prompt plans require non-empty expected and replacement text.")
        if prompt_snapshot.system_prompt.count(expected) != 1:
            raise RefinementPlannerError("Prompt plans must target text that occurs exactly once in the retrieved prompt.")
    elif target == "code":
        if operation != "replace" or payload["target_file"] not in ALLOWED_CODE_FILES:
            raise RefinementPlannerError("Code plans must replace text in an allowlisted source file.")
        if not payload["expected_current_text"].strip() or not payload["replacement_text"].strip():
            raise RefinementPlannerError("Code plans require non-empty expected and replacement text.")
    else:
        raise RefinementPlannerError("A failing evaluation requires exactly one prompt or code target.")


class RefinementPlanner:
    """Create one structured, dry-run refinement proposal from a completed run."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
        agent_config_client: ElevenLabsAgentConfigClient | Any | None = None,
        refinement_branch_id: str | None = None,
    ) -> None:
        self.model = model or OPENAI_REFINER_MODEL
        if client is None:
            key = api_key or OPENAI_API_KEY
            if not key:
                raise RefinementPlannerError("OPENAI_API_KEY is not configured in keys.env.")
            client = OpenAI(api_key=key)
        self._client = client
        self._agent_config_client = agent_config_client
        self.refinement_branch_id = refinement_branch_id or ELEVENLABS_REFINEMENT_BRANCH_ID

    def plan(self, run_id: str, deterministic: EvaluationResult | None = None) -> RefinementPlan:
        """Generate and persist one proposal without applying it anywhere."""
        events = read_run_events(run_id)
        if not events:
            raise RefinementPlannerError("The requested run transcript does not exist.")
        scenario_id = _run_scenario_id(events)
        deterministic = deterministic or evaluate_run(run_id)
        review = _review_payload(run_id)
        source_decision = str(review["decision"].get("decision"))
        prompt_snapshot: AgentPromptSnapshot | None = None
        if source_decision in {"prompt_refinement_needed", "mixed_refinement_needed"}:
            if not self.refinement_branch_id:
                raise RefinementPlannerError(
                    "Create and configure a dedicated ElevenLabs refinement branch before planning a prompt change."
                )
            try:
                prompt_client = self._agent_config_client or ElevenLabsAgentConfigClient()
                prompt_snapshot = prompt_client.get_system_prompt(branch_id=self.refinement_branch_id)
            except (ElevenLabsAgentConfigError, ValueError) as error:
                raise RefinementPlannerError("Unable to retrieve Erling's refinement-branch prompt.") from error
        append_run_event(run_id, "refinement_planning_started", scenario_id=scenario_id, model=self.model, dry_run=True)
        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=REFINER_INSTRUCTIONS,
                input=json.dumps(
                    _planning_input(
                        scenario_id=scenario_id,
                        events=events,
                        deterministic=deterministic,
                        review=review,
                        prompt_snapshot=prompt_snapshot,
                    )
                ),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "airline_refinement_plan",
                        "strict": True,
                        "schema": REFINEMENT_PLAN_SCHEMA,
                    }
                },
                reasoning={"effort": "medium"},
                max_output_tokens=1800,
                store=False,
            )
            payload = json.loads(response.output_text)
            _validate_plan_payload(
                payload,
                scenario_id=scenario_id,
                review=review,
                prompt_snapshot=prompt_snapshot,
            )
        except Exception as error:
            # The provider response itself is never written to the run log.  The
            # validation error is safe, actionable context for the operator and
            # tells us why an otherwise bounded refinement stopped.
            reason = str(error) or type(error).__name__
            append_run_event(
                run_id,
                "refinement_planning_failed",
                scenario_id=scenario_id,
                error_type=type(error).__name__,
                reason=reason,
            )
            raise RefinementPlannerError(f"Refiner could not produce a safe, valid plan: {reason}") from error

        usage = getattr(response, "usage", None)
        path = refinement_plan_path(run_id)
        plan = RefinementPlan(
            run_id=run_id,
            scenario_id=scenario_id,
            model=self.model,
            source_decision=source_decision,
            status=payload["status"],
            target=payload["target"],
            operation=payload["operation"],
            target_file=payload["target_file"],
            expected_current_text=payload["expected_current_text"],
            replacement_text=payload["replacement_text"],
            rationale=payload["rationale"],
            expected_effect=payload["expected_effect"],
            verification_scenario_id=payload["verification_scenario_id"],
            response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            plan_path=str(path),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(plan.as_dict(), indent=2) + "\n")
        append_run_event(
            run_id,
            "refinement_plan_created",
            scenario_id=scenario_id,
            model=self.model,
            source_decision=source_decision,
            status=plan.status,
            target=plan.target,
            target_file=plan.target_file,
            verification_scenario_id=plan.verification_scenario_id,
            response_id=plan.response_id,
            input_tokens=plan.input_tokens,
            output_tokens=plan.output_tokens,
            plan_path=plan.plan_path,
            dry_run=True,
        )
        return plan
