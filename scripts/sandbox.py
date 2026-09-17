"""Developer workbench for the local Ionian Airlines API.

Start the API in another terminal:
    .venv/bin/uvicorn app.ionian_api:app --reload

Enable individual calls in main() to explore, reset, or exercise the system.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

# Running this file directly makes Python treat ``scripts/`` as the import
# root. Add the project root so sandbox helpers can import application modules.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.elevenlabs_chat import ElevenLabsChatError, ElevenLabsChatSession
from app.customer_simulator import CustomerSimulator, CustomerSimulatorError
from app.evaluation import evaluate_run
from app.llm_evaluator import LLMTranscriptEvaluator, LLMTranscriptEvaluatorError
from app.run_logging import append_run_event, create_run, read_run_events, run_log_path
from app.scenarios import get_scenario
from app.simulation_runner import ScenarioRunner, SimulationRunnerError

BASE_URL = "http://127.0.0.1:8000"
load_dotenv(ROOT / "keys.env")


def print_json(label: str, value: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, indent=2))


def api_request(method: str, path: str, *, params: dict[str, object] | None = None, body: dict | None = None) -> object | None:
    """Call the running API and print a helpful error if it fails."""
    url = f"{BASE_URL}{path}"
    if params:
        url += f"?{urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if tool_token := os.getenv("IONIAN_TOOL_TOKEN"):
        headers["X-Ionian-Tool-Token"] = tool_token
    request = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(request) as response:
            return json.load(response)
    except HTTPError as error:
        print(f"\nAPI error {error.code} for {method} {path}: {error.read().decode()}")
    except URLError:
        print("\nCannot reach the API. Start it with: .venv/bin/uvicorn app.ionian_api:app --reload")
    return None


def reset_database() -> None:
    """Remove all bookings and recreate the original flight-only seed database."""
    subprocess.run([sys.executable, str(ROOT / "scripts" / "init_database.py")], check=True)


def list_policy_topics() -> object | None:
    result = api_request("GET", "/policies")
    if result:
        print_json("Policy catalogue", result)
    return result


def get_guardrails() -> object | None:
    result = api_request("GET", "/system/guardrails")
    if result:
        print_json("Configured guardrails", result)
    return result


def get_policy(topic: str) -> object | None:
    result = api_request("GET", f"/policies/{topic}")
    if result:
        print_json(f"Policy: {topic}", result)
    return result


def list_flights(destination: str | None = None, date: str | None = None, max_price_eur: int | None = None) -> list[dict]:
    params = {key: value for key, value in {"destination": destination, "date": date, "max_price_eur": max_price_eur}.items() if value is not None}
    result = api_request("GET", "/flights", params=params)
    flights = result if isinstance(result, list) else []
    print_json("Flight options", flights)
    return flights


def list_available_seats(flight_id: int) -> list[dict]:
    result = api_request("GET", f"/flights/{flight_id}/seats")
    seats = result if isinstance(result, list) else []
    print_json(f"Available seats for flight {flight_id}", seats)
    return seats


def list_bookings() -> list[dict]:
    """Print local booking records for development; deliberately bypasses the public API."""
    database_path = ROOT / "data" / "ionian_airlines.db"
    if not database_path.exists():
        print("Database missing. Run reset_database() first.")
        return []

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """SELECT
                b.reference,
                b.status,
                b.total_amount_eur,
                b.created_at_utc,
                c.full_name,
                c.email AS contact_email,
                c.loyalty_tier,
                COUNT(bs.id) AS segment_count,
                GROUP_CONCAT(
                    f.flight_number || ' ' || f.origin_iata || '-' || f.destination_iata ||
                    ' (' || bs.status || ')',
                    '; '
                ) AS segments
            FROM bookings b
            JOIN customers c ON c.id = b.primary_contact_customer_id
            LEFT JOIN booking_segments bs ON bs.booking_id = b.id
            LEFT JOIN flights f ON f.id = bs.flight_id
            GROUP BY b.id
            ORDER BY b.created_at_utc DESC, b.id DESC"""
        ).fetchall()

    bookings = [dict(row) for row in rows]
    print_json("Local development bookings", bookings)
    return bookings


def inspect_local_booking(reference: str) -> dict | None:
    """Print full local booking, segment, and ancillary data for development inspection."""
    database_path = ROOT / "data" / "ionian_airlines.db"
    if not database_path.exists():
        print("Database missing. Run reset_database() first.")
        return None

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        booking = connection.execute(
            """SELECT b.*, c.full_name, c.email AS contact_email, c.loyalty_tier
            FROM bookings b
            JOIN customers c ON c.id = b.primary_contact_customer_id
            WHERE b.reference = ?""",
            (reference.upper(),),
        ).fetchone()
        if not booking:
            print(f"No local booking found for {reference.upper()}.")
            return None

        segments = connection.execute(
            """SELECT bs.*, f.flight_number, f.origin_iata, f.destination_iata,
            f.scheduled_departure_at_utc, s.seat_number
            FROM booking_segments bs
            JOIN flights f ON f.id = bs.flight_id
            LEFT JOIN seats s ON s.id = bs.seat_id
            WHERE bs.booking_id = ?
            ORDER BY bs.id""",
            (booking["id"],),
        ).fetchall()
        ancillaries = connection.execute(
            """SELECT a.*
            FROM ancillaries a
            JOIN booking_segments bs ON bs.id = a.booking_segment_id
            WHERE bs.booking_id = ?
            ORDER BY a.id""",
            (booking["id"],),
        ).fetchall()

    ancillary_details = []
    for ancillary in ancillaries:
        item = dict(ancillary)
        item["details"] = json.loads(item.pop("details_json"))
        ancillary_details.append(item)

    result = {
        "booking": dict(booking),
        "segments": [dict(segment) for segment in segments],
        "ancillaries": ancillary_details,
    }
    print_json(f"Local booking inspection: {reference.upper()}", result)
    return result


def create_booking(*, flight_id: int, fare_tier: str, name: str = "Sandbox Customer", email: str = "sandbox@example.com", seat_number: str | None = None, loyalty_number: str | None = None) -> dict | None:
    result = api_request("POST", "/bookings", body={"customer": {"full_name": name, "email": email, "loyalty_number": loyalty_number}, "flight_id": flight_id, "fare_tier": fare_tier, "seat_number": seat_number})
    if result:
        print_json("Created booking", result)
    return result if isinstance(result, dict) else None


def create_demo_booking() -> dict | None:
    """Book the first Economy Classic Athens-to-London option in the seed schedule."""
    fare = next((flight for flight in list_flights(destination="LHR") if flight["fare_tier"] == "economy_classic"), None)
    if not fare:
        print("No Economy Classic London fare was found.")
        return None
    return create_booking(flight_id=fare["id"], fare_tier="economy_classic", name="Elena Sandbox", email="elena.sandbox@example.com")


def get_booking(reference: str, contact_email: str = "sandbox@example.com") -> dict | None:
    result = api_request("GET", f"/bookings/{reference.upper()}", params={"contact_email": contact_email})
    if result:
        print_json(f"Booking: {reference.upper()}", result)
    return result if isinstance(result, dict) else None


def add_checked_bag(reference: str, booking_segment_id: int, option: str = "23kg", contact_email: str = "sandbox@example.com") -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", params={"contact_email": contact_email}, body={"booking_segment_id": booking_segment_id, "ancillary_type": "checked_bag", "option": option})
    if result:
        print_json("Added checked bag", result)
    return result if isinstance(result, dict) else None


def add_pet(reference: str, booking_segment_id: int, *, travel_mode: str, animal_type: str, combined_weight_kg: float, contact_email: str = "sandbox@example.com") -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", params={"contact_email": contact_email}, body={"booking_segment_id": booking_segment_id, "ancillary_type": "pet", "option": travel_mode, "animal_type": animal_type, "combined_weight_kg": combined_weight_kg})
    if result:
        print_json("Added pet", result)
    return result if isinstance(result, dict) else None


def add_special_item(reference: str, booking_segment_id: int, *, option: str, weight_kg: float, contact_email: str = "sandbox@example.com") -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", params={"contact_email": contact_email}, body={"booking_segment_id": booking_segment_id, "ancillary_type": "special_item", "option": option, "weight_kg": weight_kg})
    if result:
        print_json("Added special item", result)
    return result if isinstance(result, dict) else None


def cancel_booking(reference: str, contact_email: str = "sandbox@example.com") -> object | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/cancel", params={"contact_email": contact_email}, body={"confirmation": "confirmed"})
    if result:
        print_json("Cancellation result", result)
    return result


def chat_with_erling(message: str = "Hello Erling. What is the fee for a 7 kg cat in the cabin?") -> str | None:
    """Run one paid, live Chat Mode turn against the configured ElevenLabs agent.

    The provider conversation ID is printed so the same run can be inspected in
    the ElevenLabs dashboard. This is intentionally an opt-in developer helper;
    it does not mock tools or bypass Erling's real webhook configuration.
    """
    run_id = create_run(source="sandbox_manual_chat")
    try:
        with ElevenLabsChatSession(run_id=run_id) as session:
            reply = session.send_message(message)
    except ElevenLabsChatError as error:
        append_run_event(run_id, "run_failed", error_type=type(error).__name__, message=str(error))
        print(f"\nElevenLabs Chat Mode error: {error}")
        return None
    append_run_event(
        run_id,
        "run_completed",
        conversation_id=reply.conversation_id,
    )

    print_json(
        "Erling Chat Mode reply",
        {
            "run_id": run_id,
            "transcript_path": str(run_log_path(run_id)),
            "conversation_id": reply.conversation_id,
            "initial_greeting": session.initial_greeting.text if session.initial_greeting else None,
            "agent_reply": reply.text,
            "agent_messages_for_turn": list(reply.messages),
        },
    )
    return reply.text


def inspect_run_transcript(run_id: str) -> list[dict]:
    """Print one unified simulation/manual-chat transcript for development."""
    events = read_run_events(run_id)
    print_json(f"Run transcript: {run_id}", events)
    return events


def inspect_run_evaluation(run_id: str) -> dict:
    """Evaluate a previous scenario run and print its durable assessment report."""
    result = evaluate_run(run_id)
    payload = result.as_dict()
    print_json(f"Run evaluation: {run_id}", payload)
    return payload


def review_run_with_llm(run_id: str) -> dict | None:
    """Run the paid, assessment-required independent transcript review once."""
    try:
        deterministic = evaluate_run(run_id)
        result = LLMTranscriptEvaluator().evaluate(run_id, deterministic)
    except (ValueError, LLMTranscriptEvaluatorError) as error:
        print(f"\nLLM evaluation error: {error}")
        return None
    payload = result.as_dict()
    print_json(f"LLM transcript review: {run_id}", payload)
    return payload


def preview_customer_simulator(scenario_id: str = "pet_policy_in_cabin") -> None:
    """Make one paid OpenAI call to check the customer simulator in isolation.

    This does not contact ElevenLabs or modify the airline database. It is kept
    commented out in main() so a paid call is always a deliberate choice.
    """
    scenario = get_scenario(scenario_id)
    run_id = create_run(source="sandbox_customer_simulator_preview", scenario_id=scenario.id)
    try:
        simulator = CustomerSimulator()
        turn = simulator.next_turn(scenario, [], run_id=run_id)
    except CustomerSimulatorError as error:
        append_run_event(run_id, "run_failed", error_type=type(error).__name__)
        print(f"\nCustomer simulator error: {error}")
        return
    append_run_event(run_id, "run_completed")
    print_json(
        "Customer simulator preview",
        {
            "run_id": run_id,
            "transcript_path": str(run_log_path(run_id)),
            "scenario_id": scenario.id,
            "action": turn.action,
            "message": turn.message,
            "model": simulator.model,
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
        },
    )


def simulate_scenario(scenario_id: str = "pet_policy_in_cabin", *, with_llm_review: bool = False) -> None:
    """Run one paid, end-to-end customer simulation against Erling.

    Fixture-backed action scenarios create fresh isolated test records. Set
    ``with_llm_review`` only for an assessment/refinement run, as it makes one
    additional paid OpenAI evaluator call after the conversation ends.
    """
    try:
        result = ScenarioRunner().run(scenario_id, run_llm_review=with_llm_review)
    except SimulationRunnerError as error:
        print(f"\nScenario simulation error: {error}")
        return
    print_json(
        "Scenario simulation result",
        {
            "run_id": result.run_id,
            "scenario_id": result.scenario_id,
            "outcome": result.outcome,
            "customer_turns": result.customer_turns,
            "conversation_id": result.conversation_id,
            "transcript_path": result.transcript_path,
            "evaluation": result.evaluation.as_dict() if result.evaluation else None,
            "llm_evaluation": result.llm_evaluation.as_dict() if result.llm_evaluation else None,
        },
    )


def simulate_assessment_scenario(scenario_id: str = "pet_policy_in_cabin") -> None:
    """Run a paid end-to-end assessment scenario including its LLM evaluation."""
    simulate_scenario(scenario_id, with_llm_review=True)


def main() -> None:
    # Toggle, reorder, or extend these calls while developing.
    # reset_database()  # WARNING: removes every persisted booking.

    """
    Live ElevenLabs request: uncomment only when intentionally testing.
    """ 
    # chat_with_erling()

    """
    Paid OpenAI request only; this does not contact Erling.
    """
    # preview_customer_simulator()

    """
    Paid OpenAI + ElevenLabs run. The default scenario is policy-only.
    """
    # simulate_scenario("create_business_booking")
    # simulate_assessment_scenario("reschedule_existing_booking")

    # inspect_run_transcript("run_c3005bb49bfa4cea94f6a0aa1e9769a5")

    review_run_with_llm("run_4462dc838e2d42e5b612a47c640e9e88")



if __name__ == "__main__":
    main()
