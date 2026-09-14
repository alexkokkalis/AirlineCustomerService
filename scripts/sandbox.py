"""Read-only explorer for the local Ionian Airlines API.

Start the API first: .venv/bin/uvicorn app.main:app --reload
Examples:
  python3 scripts/sandbox.py flights LHR
  python3 scripts/sandbox.py seats 21
  python3 scripts/sandbox.py booking ION-ABC123
  python3 scripts/sandbox.py policies
  python3 scripts/sandbox.py policy pets
"""

from __future__ import annotations

import argparse
import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


BASE_URL = "http://127.0.0.1:8000"


def get(path: str, params: dict[str, str] | None = None) -> object:
    url = f"{BASE_URL}{path}"
    if params:
        url += f"?{urlencode(params)}"
    try:
        with urlopen(url) as response:
            return json.load(response)
    except HTTPError as error:
        print(f"API error {error.code}: {error.read().decode()}")
    except URLError:
        print("Cannot reach the API. Start it with: .venv/bin/uvicorn app.main:app --reload")
    return None


def show_flights(destination: str | None) -> None:
    rows = get("/flights", {"destination": destination.upper()} if destination else None) or []
    print("ID   Flight  Route     Departure (UTC)       Tier              EUR  Gate")
    for flight in rows:
        print(f"{flight['id']:<4} {flight['flight_number']:<7} {flight['origin_iata']}-{flight['destination_iata']:<7} {flight['scheduled_departure_at_utc']:<22} {flight['fare_tier']:<17} {flight['base_price_eur']:<4} {flight['gate']}")


def show_seats(flight_id: int) -> None:
    rows = get(f"/flights/{flight_id}/seats") or []
    by_cabin: dict[str, list[str]] = {}
    for seat in rows:
        exit_marker = ", exit row" if seat["is_exit_row"] else ""
        by_cabin.setdefault(seat["cabin"], []).append(f"{seat['seat_number']} ({seat['position']}, {seat['seat_type']}{exit_marker})")
    for cabin, seats in by_cabin.items():
        print(f"\n{cabin.title()} available seats ({len(seats)}):")
        print(", ".join(seats))


def main() -> None:
    parser = argparse.ArgumentParser(description="Explore the local Ionian Airlines API.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    flights = subparsers.add_parser("flights", help="List fare options; optionally filter by destination IATA code.")
    flights.add_argument("destination", nargs="?", help="For example: LHR")
    seats = subparsers.add_parser("seats", help="List available seats for a flight ID.")
    seats.add_argument("flight_id", type=int)
    booking = subparsers.add_parser("booking", help="Retrieve a booking by reference.")
    booking.add_argument("reference")
    subparsers.add_parser("policies", help="List valid policy topics and their descriptions.")
    policy = subparsers.add_parser("policy", help="Retrieve one exact top-level policy topic.")
    policy.add_argument("topic", help="For example: pets or baggage")
    args = parser.parse_args()
    if args.command == "flights":
        show_flights(args.destination)
    elif args.command == "seats":
        show_seats(args.flight_id)
    elif args.command == "policy":
        result = get(f"/policies/{args.topic}")
        if result:
            print(json.dumps(result, indent=2))
    elif args.command == "policies":
        result = get("/policies")
        if result:
            print(json.dumps(result, indent=2))
    else:
        result = get(f"/bookings/{args.reference.upper()}")
        if result:
            print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
