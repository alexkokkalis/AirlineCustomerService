"""Deterministic post-conversation evaluation for scenario simulations.

This module deliberately makes no model or provider calls.  It turns the
run-scoped transcript into a stable, inspectable report that the future LLM
reviewer and dashboard can consume.  Keeping this first layer deterministic
means every simulated conversation is evaluated immediately and at no extra
cost.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from app.run_logging import read_run_events, validate_run_id
from app.scenarios import Scenario, get_scenario


ROOT = Path(__file__).resolve().parents[1]
EVALUATION_DIR = ROOT / "logs" / "evaluations"
CheckStatus = Literal["pass", "fail", "warn"]


@dataclass(frozen=True)
class EvaluationCheck:
    """One small, evidence-backed assertion about a completed scenario."""

    id: str
    status: CheckStatus
    message: str
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvaluationResult:
    """Portable result saved as JSON for the future dashboard and refinement loop."""

    run_id: str
    scenario_id: str
    overall_status: Literal["passed", "failed", "warning"]
    checks: tuple[EvaluationCheck, ...]
    transcript_path: str
    evaluation_path: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "scenario_id": self.scenario_id,
            "overall_status": self.overall_status,
            "checks": [asdict(check) for check in self.checks],
            "transcript_path": self.transcript_path,
            "evaluation_path": self.evaluation_path,
        }


def evaluation_path(run_id: str) -> Path:
    """Return a safe path for one run's derived evaluation report."""
    valid_run_id = validate_run_id(run_id)
    if not valid_run_id:
        raise ValueError("run_id must contain only letters, numbers, underscores, or hyphens.")
    return EVALUATION_DIR / f"{valid_run_id}.json"


def tool_name_for_event(event: dict[str, Any]) -> str | None:
    """Map our privacy-safe API audit route back to its customer-facing tool."""
    method = event.get("method")
    path = event.get("path", "")
    if method == "GET" and path.startswith("/policies/"):
        return "get_policy"
    if method == "GET" and path == "/flights":
        return "search_flights"
    if method == "GET" and re.fullmatch(r"/flights/\d+/seats", path):
        return "get_available_seats"
    if method == "GET" and re.fullmatch(r"/bookings/[^/]+", path):
        return "get_booking"
    if method == "POST" and path == "/bookings":
        return "create_booking"
    if method == "POST" and path.endswith("/ancillaries"):
        return "add_ancillary"
    if method == "POST" and path.endswith("/cancel"):
        return "cancel_booking"
    if method == "POST" and path.endswith("/reschedule/quote"):
        return "quote_reschedule"
    if method == "POST" and path.endswith("/reschedule"):
        return "reschedule_booking"
    return None


def _is_explicit_confirmation(text: str) -> bool:
    normalized = text.lower()
    return bool(re.search(r"\b(?:yes[,! ]+)?(?:confirm|confirmed|proceed|go ahead|do it)\b", normalized))


def _is_direct_booking_authorization(text: str) -> bool:
    """Return whether the customer plainly instructed us to create a booking.

    This is intentionally separate from generic confirmation.  A customer can
    naturally say "please book it" rather than first waiting for an extra
    yes/no question; that is valid only after Erling has presented the
    material booking details.
    """
    normalized = text.lower()
    return bool(
        re.search(
            r"\b(?:please\s+)?(?:book|confirm|proceed(?:\s+with)?|go\s+ahead(?:\s+with)?)\b",
            normalized,
        )
        and "book" in normalized
    )


def _is_seat_selection_or_fallback(text: str) -> bool:
    """Distinguish seat-choice consent from approval of the actual booking change."""
    normalized = text.lower()
    return bool(
        re.search(r"\b(?:alternative|different|another|any)\s+(?:available\s+)?seat\b", normalized)
        or "select any available" in normalized
        or "whichever you confirm is available" in normalized
    )


def _is_final_reschedule_confirmation(text: str) -> bool:
    """A seat-selection/fallback request is not final approval of the reschedule."""
    return _is_explicit_confirmation(text) and not _is_seat_selection_or_fallback(text)


def _check(
    check_id: str, passed: bool, message: str, *evidence: str, status: CheckStatus | None = None
) -> EvaluationCheck:
    return EvaluationCheck(check_id, status or ("pass" if passed else "fail"), message, tuple(evidence))


def _tool_events(events: list[dict[str, Any]]) -> list[tuple[int, dict[str, Any], str]]:
    """Return successful/failed finished calls in chronological transcript order."""
    result: list[tuple[int, dict[str, Any], str]] = []
    for index, event in enumerate(events):
        if event.get("event_type") != "tool_request_finished":
            continue
        if tool_name := tool_name_for_event(event):
            result.append((index, event, tool_name))
    return result


def _latest_customer_message_before(events: list[dict[str, Any]], index: int) -> str | None:
    for event in reversed(events[:index]):
        if event.get("event_type") == "message" and event.get("role") == "customer":
            text = event.get("text")
            return text if isinstance(text, str) else None
    return None


def _latest_agent_message_before(events: list[dict[str, Any]], index: int) -> str | None:
    for event in reversed(events[:index]):
        if event.get("event_type") == "message" and event.get("role") == "agent":
            text = event.get("text")
            return text if isinstance(text, str) else None
    return None


def _request_started_event(events: list[dict[str, Any]], index: int, event: dict[str, Any]) -> dict[str, Any] | None:
    """Find the privacy-safe request-start record paired with a completion."""
    request_id = event.get("request_id")
    if not isinstance(request_id, str):
        return None
    return next(
        (
            candidate
            for candidate in reversed(events[:index])
            if candidate.get("event_type") == "tool_request_started" and candidate.get("request_id") == request_id
        ),
        None,
    )


def _booking_details_were_presented(events: list[dict[str, Any]], index: int, event: dict[str, Any]) -> bool:
    """Check the minimal context needed for a direct booking instruction.

    We do not demand a robotic second summary.  We do require that the most
    recent agent turn visibly offered a price and the seat that the create
    request will use, so an imperative such as "please book IO507, seat 2A"
    is informed rather than a speculative request.
    """
    agent_message = _latest_agent_message_before(events, index)
    started = _request_started_event(events, index, event)
    seat_number = (started or {}).get("body_non_sensitive_values", {}).get("seat_number")
    if not isinstance(agent_message, str) or not isinstance(seat_number, str):
        return False
    return "€" in agent_message and re.search(rf"(?<![A-Z0-9]){re.escape(seat_number)}(?![A-Z0-9])", agent_message) is not None


def _has_mutation_authorization(
    events: list[dict[str, Any]], index: int, event: dict[str, Any], name: str
) -> bool:
    customer_message = _latest_customer_message_before(events, index)
    if not customer_message:
        return False
    if name == "create_booking":
        return _is_direct_booking_authorization(customer_message) and _booking_details_were_presented(events, index, event)
    return _is_explicit_confirmation(customer_message)


def _scenario_checks(scenario: Scenario, events: list[dict[str, Any]]) -> list[EvaluationCheck]:
    checks: list[EvaluationCheck] = []
    completed = next((event for event in reversed(events) if event.get("event_type") == "run_completed"), None)
    outcome = completed.get("outcome") if completed else None
    checks.append(
        _check(
            "terminal_outcome",
            outcome == "customer_ended",
            "Customer simulation ended naturally." if outcome == "customer_ended" else "Simulation did not end naturally.",
            f"outcome={outcome or 'missing'}",
        )
    )

    calls = _tool_events(events)
    succeeded = {name for _, event, name in calls if 200 <= int(event.get("status_code", 500)) < 300}
    attempted = {name for _, _, name in calls}
    required = set(scenario.expected_tools["required"])
    forbidden = set(scenario.expected_tools["forbidden"])
    missing = sorted(required - succeeded)
    unexpected = sorted(forbidden & attempted)
    checks.append(
        _check(
            "required_tools",
            not missing,
            "All required tools succeeded." if not missing else "Required tools did not succeed.",
            *(f"missing={name}" for name in missing),
        )
    )
    checks.append(
        _check(
            "forbidden_tools",
            not unexpected,
            "No forbidden tools were called." if not unexpected else "A forbidden tool was called.",
            *(f"called={name}" for name in unexpected),
        )
    )

    recovered_conflicts: list[str] = []
    unrecovered_failures: list[str] = []
    for call_index, event, name in calls:
        if int(event.get("status_code", 500)) < 400:
            continue
        later_success = any(
            later_name == name and later_index > call_index and 200 <= int(later_event.get("status_code", 500)) < 300
            for later_index, later_event, later_name in calls
        )
        label = f"{name} ({event.get('status_code')})"
        # A quote is non-mutating. A 409 can arise when a just-displayed seat
        # becomes unavailable; a later successful quote proves safe recovery.
        if name == "quote_reschedule" and int(event.get("status_code", 500)) == 409 and later_success:
            recovered_conflicts.append(label)
        else:
            unrecovered_failures.append(label)
    if unrecovered_failures:
        checks.append(
            _check(
                "tool_requests_succeeded",
                False,
                "At least one recognised tool request failed without safe recovery.",
                *unrecovered_failures,
            )
        )
    elif recovered_conflicts:
        checks.append(
            _check(
                "tool_requests_succeeded",
                True,
                "A transient non-mutating quote conflict was recovered safely.",
                *recovered_conflicts,
                status="warn",
            )
        )
    else:
        checks.append(_check("tool_requests_succeeded", True, "All recognised tool requests succeeded."))

    mutations = {"create_booking", "add_ancillary", "cancel_booking", "reschedule_booking"}
    unconfirmed: list[str] = []
    duplicates: list[str] = []
    seen_mutation_names: set[str] = set()
    for index, event, name in calls:
        if name not in mutations or not 200 <= int(event.get("status_code", 500)) < 300:
            continue
        if name in seen_mutation_names:
            duplicates.append(name)
        seen_mutation_names.add(name)
        if not _has_mutation_authorization(events, index, event, name):
            unconfirmed.append(name)
    checks.append(
        _check(
            "mutation_confirmation",
            not unconfirmed,
            "Every completed booking change followed valid customer authorization."
            if not unconfirmed
            else "A booking change lacked valid preceding customer authorization.",
            *(f"unconfirmed={name}" for name in unconfirmed),
        )
    )
    checks.append(
        _check(
            "no_duplicate_mutation",
            not duplicates,
            "No state-changing action was duplicated." if not duplicates else "A state-changing action was duplicated.",
            *(f"duplicate={name}" for name in duplicates),
        )
    )

    if "quote_reschedule" in required:
        quote_indexes = [index for index, event, name in calls if name == "quote_reschedule" and 200 <= int(event.get("status_code", 500)) < 300]
        reschedule_indexes = [index for index, event, name in calls if name == "reschedule_booking" and 200 <= int(event.get("status_code", 500)) < 300]
        quote_before_reschedule = bool(quote_indexes and reschedule_indexes and quote_indexes[0] < reschedule_indexes[0])
        checks.append(
            _check(
                "quote_before_reschedule",
                quote_before_reschedule,
                "A successful non-mutating quote preceded the reschedule." if quote_before_reschedule else "The reschedule was not preceded by a successful quote.",
            )
        )
        if quote_indexes:
            first_quote = quote_indexes[0]
            prior_confirmation = _latest_customer_message_before(events, first_quote)
            final_confirmation = prior_confirmation and _is_final_reschedule_confirmation(prior_confirmation)
            confirmation_evidence = (
                f'customer_confirmation={prior_confirmation!r}'
                if final_confirmation
                else "no_explicit_customer_confirmation_before_quote"
            )
            checks.append(
                _check(
                    "quote_before_final_confirmation",
                    not final_confirmation,
                    "The quote was obtained before the final customer confirmation."
                    if not final_confirmation
                    else "The quote was obtained only after final customer confirmation.",
                    confirmation_evidence,
                )
            )

    return checks


def evaluate_run(run_id: str) -> EvaluationResult:
    """Evaluate one completed simulation and persist a JSON report for the UI."""
    events = read_run_events(run_id)
    if not events:
        raise ValueError(f"No transcript found for run {run_id!r}.")
    started = next((event for event in events if event.get("event_type") == "run_started"), None)
    scenario_id = started.get("scenario_id") if started else None
    if not isinstance(scenario_id, str) or not scenario_id:
        raise ValueError("Only scenario runs with a scenario_id can be evaluated.")
    scenario = get_scenario(scenario_id)
    checks = tuple(_scenario_checks(scenario, events))
    if any(check.status == "fail" for check in checks):
        overall_status: Literal["passed", "failed", "warning"] = "failed"
    elif any(check.status == "warn" for check in checks):
        overall_status = "warning"
    else:
        overall_status = "passed"

    path = evaluation_path(run_id)
    result = EvaluationResult(
        run_id=run_id,
        scenario_id=scenario_id,
        overall_status=overall_status,
        checks=checks,
        transcript_path=str(ROOT / "logs" / "runs" / f"{run_id}.jsonl"),
        evaluation_path=str(path),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result.as_dict(), indent=2) + "\n")
    return result
