"""Configurable limits for live-agent refinement runs.

The autonomous loop will create one RunGuardrails instance per pipeline run
and register each scenario, conversation turn, tool call, and ElevenLabs call.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class GuardrailLimits:
    """Version-controlled safety limits for one autonomous refinement job."""

    max_iterations: int = 5
    max_scenarios_per_run: int = 3
    max_turns_per_conversation: int = 12
    max_tool_calls_per_conversation: int = 10
    max_agent_calls_per_run: int = 40

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
        self._ensure_within("scenarios", self.scenarios + 1, self.limits.max_scenarios_per_run)
        self.scenarios += 1

    def register_agent_call(self) -> None:
        self._ensure_within("agent calls", self.agent_calls + 1, self.limits.max_agent_calls_per_run)
        self.agent_calls += 1

    def validate_conversation(self, *, turns: int, tool_calls: int) -> None:
        self._ensure_within("conversation turns", turns, self.limits.max_turns_per_conversation)
        self._ensure_within("conversation tool calls", tool_calls, self.limits.max_tool_calls_per_conversation)

    @staticmethod
    def _ensure_within(label: str, actual: int, maximum: int) -> None:
        if actual > maximum:
            raise GuardrailExceeded(f"Run stopped: {label} limit of {maximum} exceeded (actual: {actual}).")
