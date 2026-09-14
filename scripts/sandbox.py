"""Developer workbench for the local Ionian Airlines API.

Start the API in another terminal:
    .venv/bin/uvicorn app.main:app --reload

Enable individual calls in main() to explore, reset, or exercise the system.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
BASE_URL = "http://127.0.0.1:8000"


def print_json(label: str, value: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(value, indent=2))


def api_request(method: str, path: str, *, params: dict[str, object] | None = None, body: dict | None = None) -> object | None:
    """Call the running API and print a helpful error if it fails."""
    url = f"{BASE_URL}{path}"
    if params:
        url += f"?{urlencode(params)}"
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {})
    try:
        with urlopen(request) as response:
            return json.load(response)
    except HTTPError as error:
        print(f"\nAPI error {error.code} for {method} {path}: {error.read().decode()}")
    except URLError:
        print("\nCannot reach the API. Start it with: .venv/bin/uvicorn app.main:app --reload")
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


def create_booking(*, flight_id: int, fare_tier: str, name: str = "Sandbox Customer", seat_number: str | None = None, loyalty_number: str | None = None) -> dict | None:
    result = api_request("POST", "/bookings", body={"customer": {"full_name": name, "loyalty_number": loyalty_number}, "flight_id": flight_id, "fare_tier": fare_tier, "seat_number": seat_number})
    if result:
        print_json("Created booking", result)
    return result if isinstance(result, dict) else None


def create_demo_booking() -> dict | None:
    """Book the first Economy Classic Athens-to-London option in the seed schedule."""
    fare = next((flight for flight in list_flights(destination="LHR") if flight["fare_tier"] == "economy_classic"), None)
    if not fare:
        print("No Economy Classic London fare was found.")
        return None
    return create_booking(flight_id=fare["id"], fare_tier="economy_classic", name="Elena Sandbox")


def get_booking(reference: str) -> dict | None:
    result = api_request("GET", f"/bookings/{reference.upper()}")
    if result:
        print_json(f"Booking: {reference.upper()}", result)
    return result if isinstance(result, dict) else None


def add_checked_bag(reference: str, booking_segment_id: int, option: str = "23kg") -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", body={"booking_segment_id": booking_segment_id, "ancillary_type": "checked_bag", "option": option})
    if result:
        print_json("Added checked bag", result)
    return result if isinstance(result, dict) else None


def add_pet(reference: str, booking_segment_id: int, *, travel_mode: str, animal_type: str, combined_weight_kg: float) -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", body={"booking_segment_id": booking_segment_id, "ancillary_type": "pet", "option": travel_mode, "animal_type": animal_type, "combined_weight_kg": combined_weight_kg})
    if result:
        print_json("Added pet", result)
    return result if isinstance(result, dict) else None


def add_special_item(reference: str, booking_segment_id: int, *, option: str, weight_kg: float) -> dict | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/ancillaries", body={"booking_segment_id": booking_segment_id, "ancillary_type": "special_item", "option": option, "weight_kg": weight_kg})
    if result:
        print_json("Added special item", result)
    return result if isinstance(result, dict) else None


def cancel_booking(reference: str) -> object | None:
    result = api_request("POST", f"/bookings/{reference.upper()}/cancel")
    if result:
        print_json("Cancellation result", result)
    return result


def main() -> None:
    # Toggle, reorder, or extend these calls while developing.
    # reset_database()  # WARNING: removes every persisted booking.

    # get_guardrails()
    # list_policy_topics()
    # list_flights(destination="LHR")

    # get_policy("pets")
    # list_available_seats(flight_id=21)
    booking = create_demo_booking()
    if booking:
        reference = booking["booking_reference"]
        segment_id = booking["booking_segment_id"]
        get_booking(reference)
        add_checked_bag(reference, segment_id, option="23kg")
        add_special_item(reference, segment_id, option="bicycle", weight_kg=18)
        add_pet(reference, segment_id, travel_mode="in_hold", animal_type="dog", combined_weight_kg=9)
        get_booking(reference)

        # cancel_booking(reference)


if __name__ == "__main__":
    main()
