"""Load and validate the versioned scenario catalogue used by agent tests."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIO_PATH = ROOT / "data" / "scenarios.json"
REQUIRED_SCENARIO_FIELDS = {
    "id",
    "title",
    "category",
    "initial_customer_message",
    "customer_goal",
    "customer_instructions",
    "fixture_id",
    "max_turns",
    "expected_tools",
    "pass_criteria",
}


@dataclass(frozen=True)
class Scenario:
    """A declarative conversation test case; no LLM or provider state is stored here."""

    id: str
    title: str
    category: str
    initial_customer_message: str
    customer_goal: str
    customer_instructions: tuple[str, ...]
    fixture_id: str | None
    max_turns: int
    expected_tools: dict[str, tuple[str, ...]]
    pass_criteria: tuple[str, ...]


def load_scenarios(path: Path = SCENARIO_PATH) -> tuple[dict, tuple[Scenario, ...]]:
    """Return metadata/fixtures plus validated, uniquely identified scenarios."""
    try:
        source = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Scenario catalogue is unavailable or invalid JSON.") from error

    fixtures = source.get("fixtures", {})
    scenarios: list[Scenario] = []
    seen_ids: set[str] = set()
    for raw in source.get("scenarios", []):
        missing = REQUIRED_SCENARIO_FIELDS - raw.keys()
        scenario_id = raw.get("id", "<unknown>")
        if missing:
            raise ValueError(f"Scenario {scenario_id!r} is missing: {', '.join(sorted(missing))}.")
        if not isinstance(scenario_id, str) or not scenario_id or scenario_id in seen_ids:
            raise ValueError(f"Scenario IDs must be unique non-empty strings; found {scenario_id!r}.")
        if raw["fixture_id"] is not None and raw["fixture_id"] not in fixtures:
            raise ValueError(f"Scenario {scenario_id!r} references unknown fixture {raw['fixture_id']!r}.")
        if not isinstance(raw["max_turns"], int) or raw["max_turns"] < 1:
            raise ValueError(f"Scenario {scenario_id!r} must have max_turns of at least one.")
        expected_tools = raw["expected_tools"]
        if not isinstance(expected_tools, dict) or not {"required", "forbidden"} <= expected_tools.keys():
            raise ValueError(f"Scenario {scenario_id!r} needs required and forbidden tool expectations.")
        seen_ids.add(scenario_id)
        scenarios.append(
            Scenario(
                id=scenario_id,
                title=raw["title"],
                category=raw["category"],
                initial_customer_message=raw["initial_customer_message"],
                customer_goal=raw["customer_goal"],
                customer_instructions=tuple(raw["customer_instructions"]),
                fixture_id=raw["fixture_id"],
                max_turns=raw["max_turns"],
                expected_tools={key: tuple(value) for key, value in expected_tools.items()},
                pass_criteria=tuple(raw["pass_criteria"]),
            )
        )
    if not scenarios:
        raise ValueError("Scenario catalogue contains no scenarios.")
    return {"metadata": source.get("metadata", {}), "fixtures": fixtures}, tuple(scenarios)


def get_scenario(scenario_id: str) -> Scenario:
    """Return one named scenario or raise a helpful error for the runner/UI."""
    _, scenarios = load_scenarios()
    for scenario in scenarios:
        if scenario.id == scenario_id:
            return scenario
    raise KeyError(f"Unknown scenario: {scenario_id}")
