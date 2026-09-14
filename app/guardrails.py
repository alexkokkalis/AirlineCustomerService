"""Configurable limits for live-agent refinement runs.

The autonomous loop will create one RunGuardrails instance per pipeline run
and register each scenario, conversation turn, tool call, and ElevenLabs call.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass


def environment_int(name: str, default: int) -> int:
    value = os.getenv(name, str(default))
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer.") from error
    if parsed < 1:
        raise ValueError(f"{name} must be at least 1.")
    return parsed


@dataclass(frozen=True)
class GuardrailLimits:
    max_iterations: int = 5
    max_scenarios_per_run: int = 3
    max_turns_per_conversation: int = 12
    max_tool_calls_per_conversation: int = 10
    max_agent_calls_per_run: int = 40

    @classmethod
    def from_environment(cls) -> "GuardrailLimits":
        return cls(
            max_iterations=environment_int("MAX_ITERATIONS", 5),
            max_scenarios_per_run=environment_int("MAX_SCENARIOS_PER_RUN", 3),
            max_turns_per_conversation=environment_int("MAX_TURNS_PER_CONVERSATION", 12),
            max_tool_calls_per_conversation=environment_int("MAX_TOOL_CALLS_PER_CONVERSATION", 10),
            max_agent_calls_per_run=environment_int("MAX_AGENT_CALLS_PER_RUN", 40),
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)


class GuardrailExceeded(RuntimeError):
    """Raised when an automated run reaches a configured safety limit."""


@dataclass
class RunGuardrails:
    limits: GuardrailLimits
    scenarios: int = 0
    agent_calls: int = 0

    def start_scenario(self) -> None:
        self.scenarios += 1
        self._ensure_within("scenarios", self.scenarios, self.limits.max_scenarios_per_run)

    def register_agent_call(self) -> None:
        self.agent_calls += 1
        self._ensure_within("agent calls", self.agent_calls, self.limits.max_agent_calls_per_run)

    def validate_conversation(self, *, turns: int, tool_calls: int) -> None:
        self._ensure_within("conversation turns", turns, self.limits.max_turns_per_conversation)
        self._ensure_within("conversation tool calls", tool_calls, self.limits.max_tool_calls_per_conversation)

    @staticmethod
    def _ensure_within(label: str, actual: int, maximum: int) -> None:
        if actual > maximum:
            raise GuardrailExceeded(f"Run stopped: {label} limit of {maximum} exceeded (actual: {actual}).")
