"""Independent LLM reviewer for completed scenario transcripts.

The deterministic evaluator establishes objective tool/audit facts. This
module supplies the assessment-required qualitative review: 1--10 scores,
specific evidence quotes, and a root-cause classification for every failure.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_EVALUATOR_MODEL
from app.customer_simulator import visible_transcript
from app.evaluation import EvaluationResult, tool_name_for_event
from app.refinement_context import REFINEMENT_SYSTEM_CONTEXT
from app.run_logging import append_run_event, read_run_events
from app.scenarios import Scenario, get_scenario


ROOT = Path(__file__).resolve().parents[1]
RootCause = Literal["prompt_issue", "code_issue", "mixed"]
CriterionId = Literal["request_understanding", "api_usage", "outcome_confirmation", "natural_end_to_end"]
CRITERIA = ("request_understanding", "api_usage", "outcome_confirmation", "natural_end_to_end")
ReviewDecision = Literal[
    "passed",
    "prompt_refinement_needed",
    "code_fix_needed",
    "mixed_refinement_needed",
]


@dataclass(frozen=True)
class CriterionReview:
    score: int
    rationale: str
    failure_quotes: tuple[str, ...]


@dataclass(frozen=True)
class FailureFinding:
    criterion: CriterionId
    quote: str
    root_cause: RootCause
    explanation: str
    recommended_change: str


@dataclass(frozen=True)
class ReviewDecisionResult:
    """Deterministic routing decision derived from the LLM's structured evidence."""

    decision: ReviewDecision
    minimum_score: int
    blocking_failures: int
    root_causes: tuple[RootCause, ...]
    reason: str


@dataclass(frozen=True)
class LLMReviewResult:
    run_id: str
    scenario_id: str
    model: str
    overall_score: int
    summary: str
    criteria: dict[CriterionId, CriterionReview]
    failures: tuple[FailureFinding, ...]
    decision: ReviewDecisionResult
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None
    review_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "model": self.model,
            "overall_score": self.overall_score,
            "summary": self.summary,
            "criteria": {key: asdict(value) for key, value in self.criteria.items()},
            "failures": [asdict(failure) for failure in self.failures],
            "decision": asdict(self.decision),
            "response_id": self.response_id,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "review_path": self.review_path,
        }


class LLMTranscriptEvaluatorError(RuntimeError):
    """Raised when an assessment review cannot be completed safely."""


REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "criteria": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                criterion: {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "rationale": {"type": "string"},
                        "failure_quotes": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["score", "rationale", "failure_quotes"],
                }
                for criterion in CRITERIA
            },
            "required": list(CRITERIA),
        },
        "failures": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "criterion": {"type": "string", "enum": list(CRITERIA)},
                    "quote": {"type": "string"},
                    "root_cause": {"type": "string", "enum": ["prompt_issue", "code_issue", "mixed"]},
                    "explanation": {"type": "string"},
                    "recommended_change": {"type": "string"},
                },
                "required": ["criterion", "quote", "root_cause", "explanation", "recommended_change"],
            },
        },
    },
    "required": ["summary", "criteria", "failures"],
}


REVIEW_INSTRUCTIONS = f"""You are an independent quality evaluator for an airline customer-service agent.
Score only the supplied evidence; never invent policy facts, tool results, hidden prompts, or parameter values.

{REFINEMENT_SYSTEM_CONTEXT}

Score each criterion from 1 to 10 and include specific short transcript or audit quotes for every failure:
1. request_understanding — did Erling understand and correctly progress the customer goal?
2. api_usage — did it use the correct API tools, in a safe order, with evidence that backend-validated calls succeeded? Treat an audit record as evidence, but do not claim unseen parameter values were correct.
3. outcome_confirmation — was the final confirmed result clear, accurate, and not overstated?
4. natural_end_to_end — was the interaction practical, coherent, and naturally completed?

For every real, material failure, add a failure entry and classify its root cause:
- prompt_issue: Erling selected the wrong action/wording or lacked a behavioural instruction. Use this only for Erling's own prompt, tool guidance, or behaviour.
- code_issue: a backend/API, webhook, fixture, customer simulator, transcript pipeline, evaluator, or other application component demonstrably malfunctioned or was inadequately implemented.
- mixed: two independent, material failures are each directly evidenced: one requires an Erling prompt change and one requires a code change. Do not use this for uncertainty or as a precaution.

Attribution boundaries for this simulated workflow:
- The customer simulator chooses the terminal `end` action and the runner closes the session. Erling is not responsible for ending a simulation merely because it has completed the customer's goal.
- If Erling has correctly fulfilled the goal and the simulated customer repeats a fulfilled request, does not choose `end`, or causes a max-turn loop, record one natural_end_to_end code_issue for the simulator/orchestration. Do not add an Erling prompt_issue that is only a symptom of that loop.
- A clear booking summary following a successful get_booking audit is adequate outcome confirmation. Do not create a prompt failure solely because Erling did not literally say that it "retrieved" the booking. Lower that score only for a material omission, contradiction, unsupported claim, or unclear result.
- For creating a booking, do not require a redundant second yes/no confirmation when Erling has already displayed the selected flight, seat, and exact price and the customer then gives a clear booking instruction such as "please book it" or "proceed with booking IO507, seat 2A." That is valid authorization. Penalize only an ambiguous request or a create_booking call made before the material selection and price were shown.
- Keep the stricter confirmation rule for cancellations and reschedules: Erling must disclose the applicable cancellation outcome or non-mutating reschedule quote before asking for, and receiving, the final approval to mutate the booking.
- Do not manufacture secondary failures from stylistic preferences. A failure must identify a concrete, material effect on correctness, safety, or completion.

Tool ownership:
- The scenario runner sends customer messages and records events; it never invokes, reorders, or synthesizes Erling's webhook calls. A tool audit event exists because Erling, through ElevenLabs, selected that tool.
- When successful tools appear in the wrong conversational order, classify this as prompt_issue: Erling selected the wrong sequence. For example, calling quote_reschedule only after asking for final confirmation is an Erling workflow failure, not orchestration.
- Use code_issue for tool sequencing only when evidence shows an application component altered, dropped, duplicated, or misrouted a request after Erling selected it, or when the backend/API itself returned incorrect data or failed unexpectedly.
- An absent audit record proves only that this run did not record the call. Do not claim the tool was never invoked when an agent message or other evidence shows that it may have been.

Refinement-loop discipline:
- Identify the most upstream, most likely cause that explains the observed failure. When one prompt or code defect plausibly explains another symptom, report only that primary cause and recommend one change for this iteration.
- Use mixed only when separate evidence proves two independent defects and each has its own concrete corrective change. Do not use mixed just because fixing either layer might help, or because the evidence is ambiguous.
- If attribution is uncertain, choose the most likely single cause (prefer code_issue when there is no direct evidence of an Erling prompt failure) and state the uncertainty. A later run can identify a remaining issue after the first fix.

Every failure must use exactly one of those three root causes; never use an unclassified/no-cause category. Use prompt_issue rather than code_issue when the backend call succeeded but Erling used it in the wrong conversational sequence. Recommended changes must be concise and actionable.

Deterministic checks are mandatory evidence. For every relevant failed deterministic check, emit at least one failure finding with a real transcript or tool-audit quote. A failed tool/order check means api_usage must score no higher than 7; do not describe that sequence as correct. A quote obtained after final confirmation is both a tool-order issue and an outcome-confirmation issue, so score both criteria consistently."""


def _decision(criteria: dict[CriterionId, CriterionReview], failures: tuple[FailureFinding, ...]) -> ReviewDecisionResult:
    """Apply the agreed quality gate independently of the LLM's prose judgment."""
    minimum_score = min(review.score for review in criteria.values())
    root_causes = tuple(sorted({failure.root_cause for failure in failures}))
    if minimum_score >= 8 and not failures:
        return ReviewDecisionResult("passed", minimum_score, 0, root_causes, "All criteria meet the score threshold and no failures were found.")
    causes = set(root_causes)
    if not failures:
        decision: ReviewDecision = "code_fix_needed"
        reason = "A criterion scored below 8 without a classified failure; the evaluator or orchestration needs automated correction."
    elif causes == {"prompt_issue"}:
        decision: ReviewDecision = "prompt_refinement_needed"
        reason = failures[0].explanation
    elif causes == {"code_issue"}:
        decision = "code_fix_needed"
        reason = failures[0].explanation
    else:
        decision = "mixed_refinement_needed"
        reason = "Independent, directly evidenced prompt and code failures require separate refinements."
    return ReviewDecisionResult(decision, minimum_score, len(failures), root_causes, reason)


def _apply_deterministic_score_caps(
    criteria: dict[CriterionId, CriterionReview], deterministic: EvaluationResult
) -> dict[CriterionId, CriterionReview]:
    """Enforce objective score ceilings independent of the review model's prose."""
    failed_checks = {check.id for check in deterministic.checks if check.status == "fail"}
    caps: dict[CriterionId, tuple[int, set[str]]] = {
        "api_usage": (
            7,
            {
                "required_tools",
                "forbidden_tools",
                "tool_requests_succeeded",
                "mutation_confirmation",
                "no_duplicate_mutation",
                "quote_before_reschedule",
                "quote_before_final_confirmation",
            },
        ),
        "outcome_confirmation": (7, {"mutation_confirmation", "quote_before_final_confirmation"}),
        "natural_end_to_end": (7, {"terminal_outcome"}),
    }
    capped = dict(criteria)
    for criterion, (maximum, check_ids) in caps.items():
        relevant = sorted(failed_checks & check_ids)
        review = capped[criterion]
        if relevant and review.score > maximum:
            capped[criterion] = CriterionReview(
                score=maximum,
                rationale=(
                    f"{review.rationale} Deterministic score cap applied because "
                    f"{', '.join(relevant)} failed."
                ),
                failure_quotes=review.failure_quotes,
            )
    return capped


def _review_path(run_id: str) -> Path:
    return ROOT / "logs" / "evaluations" / f"{run_id}.llm.json"


def _audit_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    started_by_request = {
        event.get("request_id"): event
        for event in events
        if event.get("event_type") == "tool_request_started" and event.get("request_id")
    }
    calls: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "tool_request_finished":
            continue
        tool_name = tool_name_for_event(event)
        if not tool_name:
            continue
        started = started_by_request.get(event.get("request_id"), {})
        calls.append(
            {
                "tool": tool_name,
                "method": event.get("method"),
                "path": event.get("path"),
                "status_code": event.get("status_code"),
                "query_parameter_names": started.get("query_parameter_names", []),
                "body_parameter_paths": started.get("body_parameter_paths", []),
                "body_non_sensitive_values": started.get("body_non_sensitive_values", {}),
            }
        )
    return calls


def _review_input(scenario: Scenario, events: list[dict[str, Any]], deterministic: EvaluationResult) -> dict[str, Any]:
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
        "deterministic_checks": [asdict(check) for check in deterministic.checks],
    }


class LLMTranscriptEvaluator:
    """Make the mandatory independent evaluator call after a scenario ends."""

    def __init__(self, *, model: str | None = None, api_key: str | None = None, client: Any | None = None) -> None:
        self.model = model or OPENAI_EVALUATOR_MODEL
        if client is None:
            key = api_key or OPENAI_API_KEY
            if not key:
                raise LLMTranscriptEvaluatorError("OPENAI_API_KEY is not configured in keys.env.")
            client = OpenAI(api_key=key)
        self._client = client

    def evaluate(self, run_id: str, deterministic: EvaluationResult | None = None) -> LLMReviewResult:
        events = read_run_events(run_id)
        started = next((event for event in events if event.get("event_type") == "run_started"), None)
        scenario_id = started.get("scenario_id") if started else None
        if not isinstance(scenario_id, str):
            raise LLMTranscriptEvaluatorError("A scenario_id is required for LLM evaluation.")
        scenario = get_scenario(scenario_id)
        if deterministic is None:
            from app.evaluation import evaluate_run

            deterministic = evaluate_run(run_id)
        append_run_event(run_id, "llm_evaluation_started", scenario_id=scenario_id, model=self.model)
        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=REVIEW_INSTRUCTIONS,
                input=json.dumps(_review_input(scenario, events, deterministic)),
                text={"format": {"type": "json_schema", "name": "airline_transcript_review", "strict": True, "schema": REVIEW_SCHEMA}},
                reasoning={"effort": "medium"},
                max_output_tokens=2200,
                store=False,
            )
            output_text = getattr(response, "output_text", "")
            try:
                payload = json.loads(output_text)
            except json.JSONDecodeError as error:
                incomplete_details = getattr(response, "incomplete_details", None)
                append_run_event(
                    run_id,
                    "llm_evaluation_invalid_output",
                    scenario_id=scenario_id,
                    response_id=getattr(response, "id", None),
                    output_characters=len(output_text) if isinstance(output_text, str) else 0,
                    response_status=getattr(response, "status", None),
                    incomplete_reason=(
                        getattr(incomplete_details, "reason", None)
                        if incomplete_details is not None
                        else None
                    ),
                )
                raise error
            criteria = {
                criterion: CriterionReview(
                    score=payload["criteria"][criterion]["score"],
                    rationale=payload["criteria"][criterion]["rationale"],
                    failure_quotes=tuple(payload["criteria"][criterion]["failure_quotes"]),
                )
                for criterion in CRITERIA
            }
            failures = tuple(
                FailureFinding(
                    criterion=item["criterion"],
                    quote=item["quote"],
                    root_cause=item["root_cause"],
                    explanation=item["explanation"],
                    recommended_change=item["recommended_change"],
                )
                for item in payload["failures"]
            )
        except Exception as error:
            append_run_event(run_id, "llm_evaluation_failed", scenario_id=scenario_id, error_type=type(error).__name__)
            raise LLMTranscriptEvaluatorError("LLM transcript evaluation could not produce a valid review.") from error

        criteria = _apply_deterministic_score_caps(criteria, deterministic)
        usage = getattr(response, "usage", None)
        path = _review_path(run_id)
        decision = _decision(criteria, failures)
        overall_score = round(sum(review.score for review in criteria.values()) / len(criteria))
        result = LLMReviewResult(
            run_id=run_id,
            scenario_id=scenario_id,
            model=self.model,
            overall_score=overall_score,
            summary=payload["summary"],
            criteria=criteria,
            failures=failures,
            decision=decision,
            response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            review_path=str(path),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result.as_dict(), indent=2) + "\n")
        append_run_event(
            run_id,
            "llm_evaluation_completed",
            scenario_id=scenario_id,
            model=self.model,
            response_id=result.response_id,
            overall_score=result.overall_score,
            decision=result.decision.decision,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            review_path=result.review_path,
        )
        return result
