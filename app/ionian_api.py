"""Agent-callable booking API for Ionian Airlines."""

from __future__ import annotations

import hmac
import json
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator
from starlette.responses import JSONResponse

from app.config import IONIAN_TOOL_TOKEN
from app.guardrails import GuardrailLimits
from app.run_logging import append_run_event, validate_run_id


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT / "data" / "ionian_airlines.db"
API_EVENT_LOG_PATH = ROOT / "logs" / "api_events.jsonl"
AGENT_API_PATH_PREFIXES = ("/policies", "/flights", "/bookings")
SAFE_AUDIT_VALUE_FIELDS = {
    "ancillary_type",
    "booking_segment_id",
    "combined_weight_kg",
    "confirmation",
    "fare_tier",
    "flight_id",
    "new_flight_id",
    "option",
    "seat_number",
    "weight_kg",
}
FARE_CABINS = {"economy_light": "economy", "economy_classic": "economy", "economy_plus": "economy", "business": "business"}
MULTIPLIERS = {"economy_light": 1.0, "economy_classic": 1.2, "economy_plus": 1.55, "business": 3.2}
PolicyTopic = Literal[
    "fare_tiers",
    "baggage",
    "pets",
    "special_assistance",
    "check_in_and_flight_status",
    "disruptions",
    "voluntary_changes_and_refunds",
    "ionian_loyalty",
]
POLICY_TOPIC_DESCRIPTIONS = {
    "fare_tiers": "Fare benefits, restrictions, internal multipliers, and standard add-on rules.",
    "baggage": "Cabin and checked-bag allowances, fees, limits, special items, and restricted items.",
    "pets": "Pet eligibility, carrier and weight requirements, fees, documents, and assistance dogs.",
    "special_assistance": "Mobility, medical-device, disability, and airport-assistance options and limits.",
    "check_in_and_flight_status": "Check-in deadlines, boarding, seat rules, gates, and flight-status meanings.",
    "disruptions": "Delays, cancellations, missed connections, rebooking, refunds, and compensation assessment.",
    "voluntary_changes_and_refunds": "Customer-requested changes, cancellations, refunds, travel credit, and name corrections.",
    "ionian_loyalty": "Bronze, Silver, and Gold qualification and benefits, including Gold Economy Plus pricing.",
}
AncillaryType = Literal["checked_bag", "pet", "special_item"]
AncillaryOption = Literal[
    "15kg",
    "23kg",
    "32kg",
    "in_cabin",
    "in_hold",
    "standard_sports",
    "heavy_sports",
    "bicycle",
    "oversized_sports",
    "hold_instrument",
]

app = FastAPI(title="Ionian Airlines Agent API", version="0.1.0")


def write_api_event(event: dict) -> None:
    """Append a privacy-conscious structured event without affecting API availability."""
    try:
        API_EVENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with API_EVENT_LOG_PATH.open("a") as log_file:
            log_file.write(json.dumps(event, separators=(",", ":")) + "\n")
    except OSError:
        # Logging must not break a customer-facing API request.
        pass


async def request_parameter_shape(request: Request) -> dict[str, object]:
    """Return safe parameter evidence while excluding customer data and secrets."""
    result: dict[str, object] = {
        "query_parameter_names": sorted(request.query_params.keys()),
        "body_parameter_paths": [],
        "body_non_sensitive_values": {},
    }
    if request.method not in {"POST", "PUT", "PATCH"}:
        return result
    try:
        payload = json.loads((await request.body()).decode() or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return result

    def collect(value: object, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                paths = result["body_parameter_paths"]
                assert isinstance(paths, list)
                paths.append(path)
                if key in SAFE_AUDIT_VALUE_FIELDS and isinstance(child, (str, int, float, bool)):
                    safe_values = result["body_non_sensitive_values"]
                    assert isinstance(safe_values, dict)
                    safe_values[path] = child
                collect(child, path)

    collect(payload)
    paths = result["body_parameter_paths"]
    assert isinstance(paths, list)
    paths.sort()
    return result


@app.middleware("http")
async def log_api_request(request: Request, call_next):
    """Authenticate agent tools and log method, route, status, duration, and request ID."""
    started_at = perf_counter()
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    run_id = validate_run_id(request.headers.get("X-Ionian-Run-ID"))
    status_code = 500
    error_type = None
    is_agent_route = request.url.path.startswith(AGENT_API_PATH_PREFIXES)
    parameter_shape = await request_parameter_shape(request) if run_id and is_agent_route else {}
    if run_id and is_agent_route:
        append_run_event(
            run_id,
            "tool_request_started",
            role="tool",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            **parameter_shape,
        )
    try:
        supplied_token = request.headers.get("X-Ionian-Tool-Token")
        if is_agent_route and IONIAN_TOOL_TOKEN and not (supplied_token and hmac.compare_digest(supplied_token, IONIAN_TOOL_TOKEN)):
            status_code = 401
            response = JSONResponse(status_code=401, content={"detail": "Invalid or missing agent tool token."})
            response.headers["X-Request-ID"] = request_id
            return response
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    except Exception as error:
        error_type = type(error).__name__
        raise
    finally:
        duration_ms = round((perf_counter() - started_at) * 1000, 2)
        write_api_event(
            {
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "event_type": "api_request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": duration_ms,
                "error_type": error_type,
                "agent_auth_required": bool(is_agent_route and IONIAN_TOOL_TOKEN),
            }
        )
        if run_id and is_agent_route:
            append_run_event(
                run_id,
                "tool_request_finished",
                role="tool",
                request_id=request_id,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                error_type=error_type,
            )


@contextmanager
def database():
    if not DATABASE_PATH.exists():
        raise RuntimeError("Database missing. Run: python3 scripts/init_database.py")
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


class CustomerInput(BaseModel):
    full_name: str = Field(min_length=2)
    email: str = Field(min_length=3, description="Primary contact email required for booking retrieval verification.")
    phone: str | None = None
    loyalty_number: str | None = None


class BookingInput(BaseModel):
    customer: CustomerInput
    flight_id: int
    fare_tier: Literal["economy_light", "economy_classic", "economy_plus", "business"]
    seat_number: str | None = None


class RescheduleInput(BaseModel):
    booking_segment_id: int
    new_flight_id: int
    seat_number: str | None = None


@dataclass(frozen=True)
class RescheduleQuote:
    """Validated non-mutating reschedule calculation used for quote and commit."""

    booking_reference: str
    booking_segment_id: int
    new_flight_id: int
    new_flight_number: str
    origin_iata: str
    destination_iata: str
    scheduled_departure_at_utc: str
    scheduled_arrival_at_utc: str
    seat_id: int
    seat_number: str
    fare_tier: str
    replacement_fare_eur: int
    change_fee_eur: int
    fare_difference_eur: int
    amount_due_eur: int
    applied_price_multiplier: float
    loyalty_benefit: str | None

    def public(self) -> dict:
        """Return the customer-actionable quote without database implementation details."""
        return {
            "booking_reference": self.booking_reference,
            "booking_segment_id": self.booking_segment_id,
            "new_flight": {
                "flight_number": self.new_flight_number,
                "origin_iata": self.origin_iata,
                "destination_iata": self.destination_iata,
                "scheduled_departure_at_utc": self.scheduled_departure_at_utc,
                "scheduled_arrival_at_utc": self.scheduled_arrival_at_utc,
            },
            "fare_tier": self.fare_tier,
            "seat_number": self.seat_number,
            "replacement_fare_eur": self.replacement_fare_eur,
            "change_fee_eur": self.change_fee_eur,
            "fare_difference_eur": self.fare_difference_eur,
            "amount_due_eur": self.amount_due_eur,
            "lower_fare_difference_refunded": False,
        }


class CancellationInput(BaseModel):
    confirmation: Literal["confirmed"] = Field(description="Explicit cancellation confirmation token.")


class AncillaryInput(BaseModel):
    booking_segment_id: int
    ancillary_type: AncillaryType
    option: AncillaryOption = Field(description="Valid options: checked_bag: 15kg, 23kg, 32kg; pet: in_cabin, in_hold; special_item: standard_sports, heavy_sports, bicycle, oversized_sports, hold_instrument.")
    animal_type: Literal["dog", "cat"] | None = Field(default=None, description="Required only for a pet.")
    combined_weight_kg: float | None = Field(default=None, gt=0, le=32, description="Required for a pet; includes animal and carrier or kennel.")
    weight_kg: float | None = Field(default=None, gt=0, le=32, description="Required for a special item.")

    @field_validator("animal_type", "combined_weight_kg", "weight_kg", mode="before")
    @classmethod
    def empty_optional_values_are_none(cls, value: object) -> object:
        """Accept webhook UIs that send blank optional fields as empty strings."""
        return None if value == "" else value


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def load_knowledge_base() -> dict:
    try:
        return json.loads((ROOT / "data" / "knowledge_base.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(500, "Knowledge base is unavailable.") from error


def load_policy_section(topic: PolicyTopic) -> dict:
    """Return one authoritative top-level section from the versioned policy source."""
    knowledge_base = load_knowledge_base()

    return {
        "topic": topic,
        "description": POLICY_TOPIC_DESCRIPTIONS[topic],
        "policy_version": knowledge_base["metadata"]["policy_version"],
        "effective_date": knowledge_base["metadata"]["effective_date"],
        "content": knowledge_base[topic],
    }


def policy_priced_ancillary(connection: sqlite3.Connection, segment: sqlite3.Row, request: AncillaryInput) -> tuple[str, int, dict]:
    """Validate a constrained ancillary request and return policy-owned booking data."""
    policy = load_knowledge_base()
    if request.ancillary_type == "checked_bag":
        options = {f"{item['weight_kg']}kg": item["price_eur"] for item in policy["baggage"]["checked_baggage"]["additional_bag_options"]}
        if request.option not in options:
            raise HTTPException(422, "Checked-bag option must be one of: 15kg, 23kg, 32kg.")
        weight = int(request.option.removesuffix("kg"))
        return f"One {weight} kg checked bag", options[request.option], {"weight_kg": weight}

    if request.ancillary_type == "pet":
        if request.option not in {"in_cabin", "in_hold"}:
            raise HTTPException(422, "Pet option must be in_cabin or in_hold.")
        if not request.animal_type or request.combined_weight_kg is None:
            raise HTTPException(422, "Pet requests require animal_type and combined_weight_kg.")
        departure = datetime.fromisoformat(segment["scheduled_departure_at_utc"].replace("Z", "+00:00"))
        if departure < datetime.now(timezone.utc) + timedelta(hours=48):
            raise HTTPException(409, "Pets must be booked at least 48 hours before departure.")
        cabin_policy, hold_policy = policy["pets"]["in_cabin"], policy["pets"]["in_hold"]
        if request.option == "in_cabin":
            if request.combined_weight_kg > cabin_policy["maximum_combined_weight_kg"]:
                raise HTTPException(422, "Pet and carrier exceed the 8 kg in-cabin limit; select in_hold if eligible.")
            description, amount = "Pet in cabin", cabin_policy["fee_eur_per_direction"]
            max_reservations = cabin_policy["maximum_carriers_per_flight"]
        else:
            if request.combined_weight_kg <= cabin_policy["maximum_combined_weight_kg"]:
                raise HTTPException(422, "Pets at or below 8 kg must use the in_cabin option.")
            description, amount = "Pet in hold", hold_policy["fee_eur_per_direction"]
            max_reservations = hold_policy["maximum_reservations_per_flight"]
        count = connection.execute("""SELECT COUNT(*) FROM ancillaries a JOIN booking_segments bs ON bs.id = a.booking_segment_id WHERE bs.flight_id = ? AND a.ancillary_type = 'pet' AND a.description = ? AND a.status = 'confirmed'""", (segment["flight_id"], description)).fetchone()[0]
        if count >= max_reservations:
            raise HTTPException(409, f"No remaining {request.option} pet capacity on this flight.")
        return description, amount, {"animal_type": request.animal_type, "combined_weight_kg": request.combined_weight_kg, "travel_mode": request.option}

    policy_items = {item["category"]: item for item in policy["baggage"]["special_items"]["items"]}
    special_items = {
        "standard_sports": ("Standard sports equipment", 0),
        "heavy_sports": ("Heavy sports equipment", 23),
        "bicycle": ("Bicycle", 0),
        "oversized_sports": ("Oversized sports equipment", 0),
        "hold_instrument": ("Musical instrument in hold", 0),
    }
    if request.option not in special_items:
        raise HTTPException(422, "Unsupported special-item option.")
    if request.weight_kg is None:
        raise HTTPException(422, "Special-item requests require weight_kg.")
    category, min_exclusive_weight = special_items[request.option]
    item_policy = policy_items[category]
    max_weight = item_policy["weight_limit_kg"]
    if request.weight_kg > max_weight or request.weight_kg <= min_exclusive_weight:
        raise HTTPException(422, f"{request.option} must weigh over {min_exclusive_weight} kg and no more than {max_weight} kg.")
    return category, item_policy["price_eur"], {"option": request.option, "weight_kg": request.weight_kg}


def get_customer(connection: sqlite3.Connection, customer: CustomerInput) -> sqlite3.Row:
    existing = None
    if customer.loyalty_number:
        existing = connection.execute("SELECT * FROM customers WHERE loyalty_number = ?", (customer.loyalty_number,)).fetchone()
    if existing:
        connection.execute("UPDATE customers SET full_name = ?, email = COALESCE(?, email), phone = COALESCE(?, phone) WHERE id = ?", (customer.full_name, customer.email, customer.phone, existing["id"]))
        return connection.execute("SELECT * FROM customers WHERE id = ?", (existing["id"],)).fetchone()
    cursor = connection.execute("INSERT INTO customers (full_name, email, phone, loyalty_number) VALUES (?, ?, ?, ?)", (customer.full_name, customer.email, customer.phone, customer.loyalty_number))
    return connection.execute("SELECT * FROM customers WHERE id = ?", (cursor.lastrowid,)).fetchone()


def seat_for(connection: sqlite3.Connection, flight_id: int, cabin: str, seat_number: str | None) -> sqlite3.Row:
    params: list[object] = [flight_id, cabin]
    sql = """SELECT s.* FROM seats s WHERE s.flight_id = ? AND s.cabin = ? AND NOT EXISTS (SELECT 1 FROM booking_segments bs WHERE bs.seat_id = s.id AND bs.status = 'confirmed')"""
    if seat_number:
        sql += " AND s.seat_number = ?"
        params.append(seat_number)
    else:
        sql += " ORDER BY CASE s.seat_type WHEN 'standard' THEN 0 WHEN 'preferred' THEN 1 ELSE 2 END, s.seat_number LIMIT 1"
    seat = connection.execute(sql, params).fetchone()
    if not seat:
        raise HTTPException(409, "Requested cabin or seat is unavailable.")
    return seat


def price_for(connection: sqlite3.Connection, flight_id: int, tier: str, loyalty_tier: str) -> tuple[str, float, int, str | None]:
    booked_tier, multiplier, benefit = tier, MULTIPLIERS[tier], None
    if loyalty_tier == "gold" and tier == "economy_classic":
        booked_tier, multiplier, benefit = "economy_plus", 1.2, "gold_economy_plus_at_classic_rate"
    light = connection.execute("SELECT base_price_eur FROM flight_fares WHERE flight_id = ? AND fare_tier = 'economy_light' AND is_available = 1", (flight_id,)).fetchone()
    if not light:
        raise HTTPException(409, "No purchasable fare is available for this flight.")
    return booked_tier, multiplier, round(light["base_price_eur"] * multiplier), benefit


def voluntary_change_fee(fare_tier: str) -> int:
    """Read the fare-specific voluntary-change fee from the policy source."""
    fees = load_knowledge_base()["voluntary_changes_and_refunds"]["change_fees_eur"]
    return int(fees.get(fare_tier, 0))


def calculate_reschedule_quote(
    connection: sqlite3.Connection,
    reference: str,
    request: RescheduleInput,
    contact_email: str,
) -> RescheduleQuote:
    """Validate a replacement and calculate its cost without changing a booking."""
    booking_id = verified_booking_id(connection, reference, contact_email)
    segment = connection.execute(
        """SELECT bs.*, f.origin_iata, f.destination_iata
        FROM booking_segments bs
        JOIN flights f ON f.id = bs.flight_id
        WHERE bs.booking_id = ? AND bs.id = ? AND bs.status = 'confirmed'""",
        (booking_id, request.booking_segment_id),
    ).fetchone()
    if not segment:
        raise HTTPException(404, "Confirmed booking segment not found.")
    if segment["fare_tier"] == "economy_light":
        raise HTTPException(409, "Economy Light flights cannot be changed.")
    replacement = connection.execute(
        "SELECT * FROM flights WHERE id = ? AND status = 'scheduled'", (request.new_flight_id,)
    ).fetchone()
    if not replacement:
        raise HTTPException(404, "Replacement scheduled flight not found.")
    if (replacement["origin_iata"], replacement["destination_iata"]) != (
        segment["origin_iata"],
        segment["destination_iata"],
    ):
        raise HTTPException(409, "Voluntary changes must keep the same origin and destination.")
    seat = seat_for(connection, replacement["id"], segment["cabin"], request.seat_number)
    _, multiplier, replacement_fare, benefit = price_for(
        connection,
        replacement["id"],
        segment["fare_tier"],
        "gold" if segment["loyalty_benefit"] else "none",
    )
    fare_difference = replacement_fare - segment["fare_amount_eur"]
    change_fee = voluntary_change_fee(segment["fare_tier"])
    return RescheduleQuote(
        booking_reference=reference.upper(),
        booking_segment_id=segment["id"],
        new_flight_id=replacement["id"],
        new_flight_number=replacement["flight_number"],
        origin_iata=replacement["origin_iata"],
        destination_iata=replacement["destination_iata"],
        scheduled_departure_at_utc=replacement["scheduled_departure_at_utc"],
        scheduled_arrival_at_utc=replacement["scheduled_arrival_at_utc"],
        seat_id=seat["id"],
        seat_number=seat["seat_number"],
        fare_tier=segment["fare_tier"],
        replacement_fare_eur=replacement_fare,
        change_fee_eur=change_fee,
        fare_difference_eur=fare_difference,
        amount_due_eur=change_fee + max(0, fare_difference),
        applied_price_multiplier=multiplier,
        loyalty_benefit=benefit,
    )


def verified_booking_id(connection: sqlite3.Connection, reference: str, contact_email: str) -> int:
    """Return a booking ID only when both customer-supplied verification factors match."""
    booking = connection.execute(
        """SELECT b.id
        FROM bookings b
        JOIN customers c ON c.id = b.primary_contact_customer_id
        WHERE b.reference = ? AND lower(c.email) = lower(?)""",
        (reference.upper(), contact_email.strip()),
    ).fetchone()
    if not booking:
        # Deliberately do not reveal whether the reference or email was incorrect.
        raise HTTPException(404, "Booking not found or contact details do not match.")
    return booking["id"]


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "database": DATABASE_PATH.exists()}


@app.get("/system/guardrails")
def get_guardrails() -> dict:
    """Expose non-sensitive refinement limits for development and monitoring."""
    return {"limits": GuardrailLimits().as_dict()}


@app.get("/policies")
def list_policy_topics() -> dict:
    """Return the complete allowed topic catalogue for the get_policy tool."""
    metadata = load_knowledge_base()["metadata"]
    return {
        "policy_version": metadata["policy_version"],
        "effective_date": metadata["effective_date"],
        "topics": [{"topic": topic, "description": description} for topic, description in POLICY_TOPIC_DESCRIPTIONS.items()],
    }


@app.get("/policies/{topic}")
def get_policy(topic: PolicyTopic) -> dict:
    """Retrieve one authoritative policy topic. Topic is restricted to the published catalogue."""
    return load_policy_section(topic)


@app.get("/flights")
def search_flights(
    destination: str | None = Query(default=None, min_length=3, max_length=3),
    date: str | None = Query(default=None, description="UTC date in YYYY-MM-DD format"),
    max_price_eur: int | None = Query(default=None, ge=1),
    fare_tier: str | None = Query(default=None),
) -> list[dict]:
    with database() as connection:
        query = """SELECT f.id, f.flight_number, f.origin_iata, f.destination_iata, f.scheduled_departure_at_utc, f.scheduled_arrival_at_utc, f.status, f.gate, ff.fare_tier, ff.cabin, ff.base_price_eur FROM flights f JOIN flight_fares ff ON ff.flight_id = f.id WHERE f.status = 'scheduled' AND ff.is_available = 1"""
        params: list[object] = []
        if destination:
            query += " AND f.destination_iata = ?"
            params.append(destination.upper())
        if date:
            query += " AND substr(f.scheduled_departure_at_utc, 1, 10) = ?"
            params.append(date)
        if max_price_eur is not None:
            query += " AND ff.base_price_eur <= ?"
            params.append(max_price_eur)
        if fare_tier:
            query += " AND ff.fare_tier = ?"
            params.append(fare_tier)
        query += " ORDER BY f.scheduled_departure_at_utc, ff.base_price_eur"
        return [dict(row) for row in connection.execute(query, params).fetchall()]


@app.get("/flights/{flight_id}/seats")
def list_seats(flight_id: int, available_only: bool = True) -> list[dict]:
    with database() as connection:
        flight = connection.execute("SELECT id FROM flights WHERE id = ?", (flight_id,)).fetchone()
        if not flight:
            raise HTTPException(404, "Flight not found.")
        query = """SELECT s.seat_number, s.cabin, s.seat_type, s.position, s.is_exit_row, CASE WHEN EXISTS (SELECT 1 FROM booking_segments bs WHERE bs.seat_id = s.id AND bs.status = 'confirmed') THEN 0 ELSE 1 END AS is_available FROM seats s WHERE s.flight_id = ?"""
        if available_only:
            query += " AND NOT EXISTS (SELECT 1 FROM booking_segments bs WHERE bs.seat_id = s.id AND bs.status = 'confirmed')"
        query += " ORDER BY s.cabin, s.seat_number"
        return [dict(row) for row in connection.execute(query, (flight_id,)).fetchall()]


@app.post("/bookings", status_code=201)
def book_flight(request: BookingInput) -> dict:
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        flight = connection.execute("SELECT * FROM flights WHERE id = ? AND status = 'scheduled'", (request.flight_id,)).fetchone()
        if not flight:
            raise HTTPException(404, "Scheduled flight not found.")
        customer = get_customer(connection, request.customer)
        tier, multiplier, amount, benefit = price_for(connection, request.flight_id, request.fare_tier, customer["loyalty_tier"])
        seat = seat_for(connection, request.flight_id, FARE_CABINS[tier], request.seat_number)
        reference = f"ION-{uuid.uuid4().hex[:6].upper()}"
        booking = connection.execute("INSERT INTO bookings (reference, primary_contact_customer_id, total_amount_eur) VALUES (?, ?, ?)", (reference, customer["id"], amount))
        segment = connection.execute("""INSERT INTO booking_segments (booking_id, customer_id, flight_id, seat_id, fare_tier, cabin, fare_amount_eur, standard_price_multiplier, applied_price_multiplier, loyalty_benefit) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (booking.lastrowid, customer["id"], request.flight_id, seat["id"], tier, FARE_CABINS[tier], amount, MULTIPLIERS[tier], multiplier, benefit))
        return {"booking_reference": reference, "booking_segment_id": segment.lastrowid, "flight_id": request.flight_id, "fare_tier": tier, "seat_number": seat["seat_number"], "amount_eur": amount, "loyalty_benefit": benefit}


@app.get("/bookings/{reference}")
def retrieve_booking(reference: str, contact_email: str = Query(min_length=3, description="Email address of the booking's primary contact.")) -> dict:
    """Retrieve all service-relevant booking records after verifying both contact factors."""
    with database() as connection:
        booking_id = verified_booking_id(connection, reference, contact_email)
        booking = connection.execute(
            """SELECT b.reference, b.status, b.total_amount_eur, c.full_name, c.loyalty_tier
            FROM bookings b
            JOIN customers c ON c.id = b.primary_contact_customer_id
            WHERE b.id = ?""",
            (booking_id,),
        ).fetchone()
        segments = connection.execute(
            """SELECT bs.*, f.flight_number, f.origin_iata, f.destination_iata, f.scheduled_departure_at_utc, s.seat_number
            FROM booking_segments bs
            JOIN flights f ON f.id = bs.flight_id
            LEFT JOIN seats s ON s.id = bs.seat_id
            WHERE bs.booking_id = ?""",
            (booking_id,),
        ).fetchall()
        ancillaries = connection.execute(
            """SELECT a.* FROM ancillaries a
            JOIN booking_segments bs ON bs.id = a.booking_segment_id
            WHERE bs.booking_id = ?
            ORDER BY a.id""",
            (booking_id,),
        ).fetchall()
        assistance_requests = connection.execute(
            """SELECT ar.* FROM assistance_requests ar
            JOIN booking_segments bs ON bs.id = ar.booking_segment_id
            WHERE bs.booking_id = ?
            ORDER BY ar.id""",
            (booking_id,),
        ).fetchall()
        refunds = connection.execute(
            """SELECT r.* FROM refunds r
            JOIN booking_segments bs ON bs.id = r.booking_segment_id
            WHERE bs.booking_id = ?
            ORDER BY r.id""",
            (booking_id,),
        ).fetchall()

        def decode_details(rows: list[sqlite3.Row]) -> list[dict]:
            records = []
            for row in rows:
                record = dict(row)
                record["details"] = json.loads(record.pop("details_json"))
                records.append(record)
            return records

        return {
            "booking": dict(booking),
            "segments": [dict(segment) for segment in segments],
            "ancillaries": decode_details(ancillaries),
            "assistance_requests": decode_details(assistance_requests),
            "refunds": [dict(refund) for refund in refunds],
        }


@app.post("/bookings/{reference}/ancillaries", status_code=201)
def add_ancillary(reference: str, request: AncillaryInput, contact_email: str = Query(min_length=3, description="Email address of the booking's primary contact.")) -> dict:
    with database() as connection:
        booking_id = verified_booking_id(connection, reference, contact_email)
        segment = connection.execute("""SELECT bs.id, bs.flight_id, f.scheduled_departure_at_utc FROM booking_segments bs JOIN flights f ON f.id = bs.flight_id WHERE bs.booking_id = ? AND bs.id = ? AND bs.status = 'confirmed'""", (booking_id, request.booking_segment_id)).fetchone()
        if not segment:
            raise HTTPException(404, "Confirmed booking segment not found.")
        description, amount, details = policy_priced_ancillary(connection, segment, request)
        cursor = connection.execute("INSERT INTO ancillaries (booking_segment_id, ancillary_type, description, amount_eur, details_json) VALUES (?, ?, ?, ?, ?)", (request.booking_segment_id, request.ancillary_type, description, amount, json.dumps(details)))
        connection.execute("UPDATE bookings SET total_amount_eur = total_amount_eur + ?, updated_at_utc = CURRENT_TIMESTAMP WHERE reference = ?", (amount, reference.upper()))
        return {"ancillary_id": cursor.lastrowid, "ancillary_type": request.ancillary_type, "option": request.option, "description": description, "status": "confirmed", "amount_eur": amount, "policy_version": load_knowledge_base()["metadata"]["policy_version"]}


@app.post("/bookings/{reference}/cancel")
def cancel_booking(reference: str, request: CancellationInput, contact_email: str = Query(min_length=3, description="Email address of the booking's primary contact.")) -> dict:
    with database() as connection:
        booking_id = verified_booking_id(connection, reference, contact_email)
        segments = connection.execute("SELECT bs.* FROM booking_segments bs WHERE bs.booking_id = ? AND bs.status = 'confirmed'", (booking_id,)).fetchall()
        if not segments:
            raise HTTPException(404, "No confirmed segments found for this booking.")
        outcomes = []
        for segment in segments:
            if segment["fare_tier"] == "economy_light": refund_type, amount = "voluntary_refund", 0
            elif segment["fare_tier"] == "economy_classic": refund_type, amount = "travel_credit", max(0, segment["fare_amount_eur"] - 60)
            elif segment["fare_tier"] == "economy_plus": refund_type, amount = "voluntary_refund", max(0, segment["fare_amount_eur"] - 30)
            else: refund_type, amount = "voluntary_refund", segment["fare_amount_eur"]
            connection.execute("UPDATE booking_segments SET status = 'cancelled', seat_id = NULL WHERE id = ?", (segment["id"],))
            connection.execute("INSERT INTO refunds (booking_segment_id, refund_type, amount_eur, reason, status) VALUES (?, ?, ?, 'Voluntary cancellation', 'approved')", (segment["id"], refund_type, amount))
            outcomes.append({"booking_segment_id": segment["id"], "refund_type": refund_type, "amount_eur": amount})
        connection.execute("UPDATE bookings SET status = 'cancelled', updated_at_utc = CURRENT_TIMESTAMP WHERE reference = ?", (reference.upper(),))
        return {"booking_reference": reference.upper(), "status": "cancelled", "outcomes": outcomes}


@app.post("/bookings/{reference}/reschedule")
def reschedule_booking(reference: str, request: RescheduleInput, contact_email: str = Query(min_length=3, description="Email address of the booking's primary contact.")) -> dict:
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        quote = calculate_reschedule_quote(connection, reference, request, contact_email)
        connection.execute(
            """UPDATE booking_segments
            SET flight_id = ?, seat_id = ?, fare_amount_eur = ?, applied_price_multiplier = ?,
                loyalty_benefit = ?, status = 'confirmed'
            WHERE id = ?""",
            (
                quote.new_flight_id,
                quote.seat_id,
                quote.replacement_fare_eur,
                quote.applied_price_multiplier,
                quote.loyalty_benefit,
                quote.booking_segment_id,
            ),
        )
        connection.execute(
            "UPDATE bookings SET total_amount_eur = total_amount_eur + ?, updated_at_utc = CURRENT_TIMESTAMP WHERE reference = ?",
            (quote.amount_due_eur, quote.booking_reference),
        )
        return {
            "booking_reference": quote.booking_reference,
            "booking_segment_id": quote.booking_segment_id,
            "new_flight_id": quote.new_flight_id,
            "seat_number": quote.seat_number,
            "change_fee_eur": quote.change_fee_eur,
            "fare_difference_eur": quote.fare_difference_eur,
            "amount_due_eur": quote.amount_due_eur,
        }


@app.post("/bookings/{reference}/reschedule/quote")
def quote_reschedule(reference: str, request: RescheduleInput, contact_email: str = Query(min_length=3, description="Email address of the booking's primary contact.")) -> dict:
    """Return a validated, current reschedule quote without changing the booking."""
    with database() as connection:
        return calculate_reschedule_quote(connection, reference, request, contact_email).public()
