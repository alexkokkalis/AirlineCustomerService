"""Orchestrate a scenario between the OpenAI customer and Erling.

The runner owns the turn loop. ElevenLabs owns Erling's conversational state;
the OpenAI customer receives a fresh, explicitly supplied visible transcript on
each turn. Both providers and the API webhook middleware append events to the
same run-scoped JSONL file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from app.customer_simulator import CustomerSimulator, CustomerSimulatorError
from app.elevenlabs_chat import ElevenLabsChatError, ElevenLabsChatSession
from app.run_logging import append_run_event, create_run, read_run_events, run_log_path
from app.scenarios import get_scenario


class SimulationRunnerError(RuntimeError):
    """Raised when a scenario cannot be safely run to a terminal state."""


class ChatSession(Protocol):
    """Small boundary that makes the real Chat Mode transport replaceable in tests."""

    conversation_id: str | None

    def __enter__(self) -> "ChatSession": ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None: ...

    def send_message(self, customer_message: str) -> Any: ...


@dataclass(frozen=True)
class SimulationResult:
    """Summary of one completed, failed, or safely bounded scenario run."""

    run_id: str
    scenario_id: str
    outcome: str
    customer_turns: int
    conversation_id: str | None
    transcript_path: str


class ScenarioRunner:
    """Run one scenario without allowing an unbounded customer-agent loop."""

    def __init__(
        self,
        *,
        simulator: CustomerSimulator | Any | None = None,
        session_factory: Callable[..., ChatSession] = ElevenLabsChatSession,
    ) -> None:
        self._simulator = simulator or CustomerSimulator()
        self._session_factory = session_factory

    def run(
        self,
        scenario_id: str,
        *,
        private_facts: dict[str, Any] | None = None,
    ) -> SimulationResult:
        """Run a deterministic opening then alternate one customer turn at a time.

        Fixture creation intentionally remains outside this class. A later test
        setup layer will create an isolated booking and pass its reference/email
        as ``private_facts``. This prevents a scenario runner from silently
        creating or mutating airline records before that fixture contract exists.
        """
        scenario = get_scenario(scenario_id)
        if scenario.fixture_id and not private_facts:
            raise SimulationRunnerError(
                f"Scenario {scenario_id!r} requires fixture facts for {scenario.fixture_id!r}."
            )

        run_id = create_run(source="scenario_simulation", scenario_id=scenario.id)
        append_run_event(
            run_id,
            "simulation_configured",
            scenario_id=scenario.id,
            max_customer_turns=scenario.max_turns,
            has_private_facts=bool(private_facts),
        )
        customer_turns = 0
        conversation_id: str | None = None
        outcome = "failed"

        try:
            with self._session_factory(run_id=run_id) as session:
                # Fixed test data means the initial customer turn is reproducible
                # and does not require an additional OpenAI API request.
                session.send_message(scenario.initial_customer_message)
                customer_turns = 1

                while customer_turns < scenario.max_turns:
                    turn = self._simulator.next_turn(
                        scenario,
                        read_run_events(run_id),
                        private_facts=private_facts,
                        run_id=run_id,
                    )
                    if turn.action == "end":
                        outcome = "customer_ended"
                        append_run_event(
                            run_id,
                            "simulation_ended_by_customer",
                            scenario_id=scenario.id,
                            customer_turns=customer_turns,
                        )
                        break

                    session.send_message(turn.message)
                    customer_turns += 1
                else:
                    outcome = "max_turns_reached"
                    append_run_event(
                        run_id,
                        "simulation_turn_limit_reached",
                        scenario_id=scenario.id,
                        max_customer_turns=scenario.max_turns,
                    )
                conversation_id = session.conversation_id
        except (CustomerSimulatorError, ElevenLabsChatError, OSError, ValueError) as error:
            append_run_event(
                run_id,
                "run_failed",
                scenario_id=scenario.id,
                error_type=type(error).__name__,
            )
            raise SimulationRunnerError(f"Scenario {scenario.id!r} did not complete.") from error

        append_run_event(
            run_id,
            "run_completed",
            scenario_id=scenario.id,
            outcome=outcome,
            customer_turns=customer_turns,
            conversation_id=conversation_id,
        )
        return SimulationResult(
            run_id=run_id,
            scenario_id=scenario.id,
            outcome=outcome,
            customer_turns=customer_turns,
            conversation_id=conversation_id,
            transcript_path=str(run_log_path(run_id)),
        )
