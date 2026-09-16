"""Create isolated local bookings for fixture-backed simulation scenarios.

This is test infrastructure, not an agent capability. It calls the same local
Ionian API that Erling's webhook tools use, but it deliberately does not attach
the run ID header: fixture setup must not be mistaken for an agent tool call in
the customer-facing timeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from app.config import IONIAN_TOOL_TOKEN
from app.run_logging import append_run_event
from app.scenarios import Scenario, load_scenarios


DEFAULT_API_BASE_URL = "http://127.0.0.1:8000"
FARE_CABINS = {
    "economy_light": "economy",
    "economy_classic": "economy",
    "economy_plus": "economy",
    "business": "business",
}


class ScenarioFixtureError(RuntimeError):
    """Raised when a fresh fixture booking cannot be prepared."""


@dataclass(frozen=True)
class ProvisionedFixture:
    """The run-private facts a simulated customer may reveal when asked."""

    fixture_id: str
    booking_reference: str
    contact_email: str
    full_name: str
    booking_segment_id: int
    flight_id: int
    fare_tier: str
    seat_number: str

    def private_facts(self) -> dict[str, str]:
        """Return only facts a customer could legitimately provide to Erling."""
        return {
            "booking_reference": self.booking_reference,
            "primary_contact_email": self.contact_email,
            "customer_full_name": self.full_name,
        }


class ScenarioFixtureProvisioner:
    """Provision fresh bookings through the running local API for one scenario."""

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_API_BASE_URL,
        tool_token: str | None = IONIAN_TOOL_TOKEN,
        request_json: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.tool_token = tool_token
        self._request_json = request_json or self._http_request_json

    def provision(self, scenario: Scenario, *, run_id: str) -> ProvisionedFixture | None:
        """Create the scenario's fixture booking and return its private facts."""
        if not scenario.fixture_id:
            return None
        metadata, _ = load_scenarios()
        try:
            fixture = metadata["fixtures"][scenario.fixture_id]
            customer = fixture["customer"]
            booking = fixture["booking"]
            full_name = customer["full_name"]
            contact_email = customer["email"]
            flight_number = booking["flight_number"]
            fare_tier = booking["fare_tier"]
        except (KeyError, TypeError) as error:
            raise ScenarioFixtureError(f"Fixture {scenario.fixture_id!r} is malformed.") from error

        append_run_event(run_id, "fixture_provisioning_started", fixture_id=scenario.fixture_id)
        try:
            fares = self._request_json("GET", "/flights")
            selected_fare = next(
                (
                    fare
                    for fare in fares
                    if fare["flight_number"] == flight_number and fare["fare_tier"] == fare_tier
                ),
                None,
            )
            if not selected_fare:
                raise ScenarioFixtureError(f"No scheduled {fare_tier} fare exists for {flight_number}.")
            flight_id = selected_fare["id"]

            available_seats = self._request_json("GET", f"/flights/{flight_id}/seats")
            requested_seat = booking.get("seat_number")
            cabin = FARE_CABINS.get(fare_tier)
            selected_seat = next(
                (
                    seat
                    for seat in available_seats
                    if seat["seat_number"] == requested_seat and seat["cabin"] == cabin
                ),
                None,
            )
            if not selected_seat:
                selected_seat = next((seat for seat in available_seats if seat["cabin"] == cabin), None)
            if not selected_seat:
                raise ScenarioFixtureError(f"No available {cabin} seat exists for {flight_number}.")

            created = self._request_json(
                "POST",
                "/bookings",
                body={
                    "customer": {"full_name": full_name, "email": contact_email},
                    "flight_id": flight_id,
                    "fare_tier": fare_tier,
                    "seat_number": selected_seat["seat_number"],
                },
            )
            provisioned = ProvisionedFixture(
                fixture_id=scenario.fixture_id,
                booking_reference=created["booking_reference"],
                contact_email=contact_email,
                full_name=full_name,
                booking_segment_id=created["booking_segment_id"],
                flight_id=flight_id,
                fare_tier=fare_tier,
                seat_number=created["seat_number"],
            )
        except (KeyError, TypeError, URLError, HTTPError) as error:
            raise ScenarioFixtureError(f"Unable to prepare fixture {scenario.fixture_id!r}.") from error

        append_run_event(
            run_id,
            "fixture_provisioned",
            fixture_id=provisioned.fixture_id,
            flight_number=flight_number,
            fare_tier=provisioned.fare_tier,
            seat_number=provisioned.seat_number,
        )
        return provisioned

    def _http_request_json(self, method: str, path: str, *, body: dict | None = None) -> Any:
        """Make an authenticated request to the running local Ionian API."""
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data else {}
        if self.tool_token:
            headers["X-Ionian-Tool-Token"] = self.tool_token
        request = Request(f"{self.base_url}{path}", data=data, method=method, headers=headers)
        try:
            with urlopen(request, timeout=10) as response:
                return json.load(response)
        except HTTPError as error:
            detail = error.read().decode(errors="replace")
            raise ScenarioFixtureError(f"Fixture API request {method} {path} failed ({error.code}): {detail}") from error
        except URLError as error:
            raise ScenarioFixtureError(
                "Cannot reach the local Ionian API. Start uvicorn before a fixture-backed simulation."
            ) from error
