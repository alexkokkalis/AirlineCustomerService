"""OpenAI-backed customer role player for repeatable assessment simulations.

The simulator deliberately has no policy, tool, or Erling-system-prompt
knowledge.  It sees only a scenario, optional scenario-private facts supplied
by the runner, and customer-visible messages already exchanged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal

from openai import OpenAI

from app.config import OPENAI_API_KEY, OPENAI_SIMULATOR_MODEL
from app.run_logging import append_run_event
from app.scenarios import Scenario


class CustomerSimulatorError(RuntimeError):
    """Raised when the simulator cannot produce a safe next customer turn."""


@dataclass(frozen=True)
class CustomerTurn:
    """One customer message or a decision that the simulated conversation ends."""

    action: Literal["message", "end"]
    message: str | None
    response_id: str | None
    input_tokens: int | None
    output_tokens: int | None


CUSTOMER_TURN_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["message", "end"]},
        "message": {"type": ["string", "null"]},
    },
    "required": ["action", "message"],
}


def _instructions(scenario: Scenario) -> str:
    instructions = "\n".join(f"- {item}" for item in scenario.customer_instructions)
    return f"""You are role-playing a single airline customer in a controlled test.
Stay in character. You are not an assistant, evaluator, developer, or agent.

Scenario: {scenario.title}
Customer goal: {scenario.customer_goal}
Customer behaviour:
{instructions}

You only know the scenario, any private facts supplied in the user input, and
the visible conversation transcript. Do not invent booking references, emails,
policy rules, prices, tool results, account facts, or hidden system details.

Before choosing a turn, compare the latest agent message with the customer
goal and instructions. Choose ``end`` immediately when the latest agent answer
has met the goal, even if it closes with an offer of further help. Do not repeat
a request that the agent has already answered. Do not repeat a booking reference
or email after the agent has successfully used it, unless the agent explicitly
asks for it again. Send another customer message only when a required fact is
missing, a direct clarification is needed, or the scenario requires a further
explicit confirmation.

Reply with exactly one natural, concise customer message, or end only when the
goal has clearly been met or cannot reasonably progress. Do not say that you
are a simulation. Do not explain your reasoning.
"""


def visible_transcript(events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Extract only customer-visible messages from a chronological run log."""
    transcript: list[dict[str, str]] = []
    for event in events:
        if event.get("event_type") != "message":
            continue
        role = event.get("role")
        text = event.get("text")
        if role in {"customer", "agent"} and isinstance(text, str) and text.strip():
            transcript.append({"role": role, "text": text.strip()})
    return transcript


class CustomerSimulator:
    """Produce the next customer turn using the OpenAI Responses API."""

    def __init__(
        self,
        *,
        model: str | None = None,
        api_key: str | None = None,
        client: Any | None = None,
    ) -> None:
        self.model = model or OPENAI_SIMULATOR_MODEL
        if client is None:
            key = api_key or OPENAI_API_KEY
            if not key:
                raise CustomerSimulatorError("OPENAI_API_KEY is not configured in keys.env.")
            client = OpenAI(api_key=key)
        self._client = client

    def next_turn(
        self,
        scenario: Scenario,
        transcript_events: list[dict[str, Any]],
        *,
        private_facts: dict[str, Any] | None = None,
        run_id: str | None = None,
    ) -> CustomerTurn:
        """Return exactly one customer message or an explicit end decision."""
        payload = {
            "private_facts": private_facts or {},
            "visible_transcript": visible_transcript(transcript_events),
        }
        append_run_event(
            run_id,
            "customer_simulator_started",
            scenario_id=scenario.id,
            model=self.model,
        )
        try:
            response = self._client.responses.create(
                model=self.model,
                instructions=_instructions(scenario),
                input=json.dumps(payload),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "customer_turn",
                        "strict": True,
                        "schema": CUSTOMER_TURN_SCHEMA,
                    }
                },
                reasoning={"effort": "minimal"},
                max_output_tokens=160,
                store=False,
            )
            result = json.loads(response.output_text)
            action = result.get("action")
            message = result.get("message")
            if action not in {"message", "end"}:
                raise ValueError("The simulator returned an unknown action.")
            if action == "message" and (not isinstance(message, str) or not message.strip()):
                raise ValueError("The simulator returned an empty customer message.")
            # The model occasionally accompanies an end decision with a polite
            # closing string. The action is the protocol signal, so normalise
            # that non-semantic text instead of failing an otherwise complete
            # customer simulation.
            if action == "end" and message is not None:
                append_run_event(
                    run_id,
                    "customer_simulator_end_message_ignored",
                    scenario_id=scenario.id,
                )
                message = None
        except Exception as error:
            append_run_event(
                run_id,
                "customer_simulator_failed",
                scenario_id=scenario.id,
                error_type=type(error).__name__,
                failure_reason=str(error),
            )
            raise CustomerSimulatorError("Customer simulator could not produce a valid turn.") from error

        usage = getattr(response, "usage", None)
        turn = CustomerTurn(
            action=action,
            message=message.strip() if isinstance(message, str) else None,
            response_id=getattr(response, "id", None),
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
        append_run_event(
            run_id,
            "customer_simulator_completed",
            scenario_id=scenario.id,
            model=self.model,
            response_id=turn.response_id,
            action=turn.action,
            input_tokens=turn.input_tokens,
            output_tokens=turn.output_tokens,
        )
        return turn
