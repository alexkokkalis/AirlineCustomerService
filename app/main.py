"""Agent-callable booking API for Ionian Airlines."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field


ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = ROOT / "data" / "ionian_airlines.db"
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

app = FastAPI(title="Ionian Airlines Agent API", version="0.1.0")


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
    email: str | None = None
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


class AncillaryInput(BaseModel):
    booking_segment_id: int
    ancillary_type: Literal["checked_bag", "special_item", "pet", "seat_selection"]
    description: str = Field(min_length=3)
    amount_eur: int = Field(ge=0)
    details: dict = Field(default_factory=dict)


def row_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row else None


def load_policy_section(topic: PolicyTopic) -> dict:
    """Return one authoritative top-level section from the versioned policy source."""
    try:
        knowledge_base = json.loads((ROOT / "data" / "knowledge_base.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(500, "Knowledge base is unavailable.") from error

    return {
        "topic": topic,
        "description": POLICY_TOPIC_DESCRIPTIONS[topic],
        "policy_version": knowledge_base["metadata"]["policy_version"],
        "effective_date": knowledge_base["metadata"]["effective_date"],
        "content": knowledge_base[topic],
    }


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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "database": DATABASE_PATH.exists()}


@app.get("/policies")
def list_policy_topics() -> dict:
    """Return the complete allowed topic catalogue for the get_policy tool."""
    try:
        metadata = json.loads((ROOT / "data" / "knowledge_base.json").read_text())["metadata"]
    except (OSError, json.JSONDecodeError) as error:
        raise HTTPException(500, "Knowledge base is unavailable.") from error
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
def retrieve_booking(reference: str) -> dict:
    with database() as connection:
        booking = connection.execute("""SELECT b.reference, b.status, b.total_amount_eur, c.full_name, c.loyalty_tier FROM bookings b JOIN customers c ON c.id = b.primary_contact_customer_id WHERE b.reference = ?""", (reference.upper(),)).fetchone()
        if not booking:
            raise HTTPException(404, "Booking not found.")
        segments = connection.execute("""SELECT bs.*, f.flight_number, f.origin_iata, f.destination_iata, f.scheduled_departure_at_utc, s.seat_number FROM booking_segments bs JOIN flights f ON f.id = bs.flight_id LEFT JOIN seats s ON s.id = bs.seat_id WHERE bs.booking_id = (SELECT id FROM bookings WHERE reference = ?)""", (reference.upper(),)).fetchall()
        return {"booking": dict(booking), "segments": [dict(s) for s in segments]}


@app.post("/bookings/{reference}/ancillaries", status_code=201)
def add_ancillary(reference: str, request: AncillaryInput) -> dict:
    with database() as connection:
        segment = connection.execute("""SELECT bs.id FROM booking_segments bs JOIN bookings b ON b.id = bs.booking_id WHERE b.reference = ? AND bs.id = ? AND bs.status = 'confirmed'""", (reference.upper(), request.booking_segment_id)).fetchone()
        if not segment:
            raise HTTPException(404, "Confirmed booking segment not found.")
        cursor = connection.execute("INSERT INTO ancillaries (booking_segment_id, ancillary_type, description, amount_eur, details_json) VALUES (?, ?, ?, ?, ?)", (request.booking_segment_id, request.ancillary_type, request.description, request.amount_eur, json.dumps(request.details)))
        connection.execute("UPDATE bookings SET total_amount_eur = total_amount_eur + ?, updated_at_utc = CURRENT_TIMESTAMP WHERE reference = ?", (request.amount_eur, reference.upper()))
        return {"ancillary_id": cursor.lastrowid, "status": "confirmed", "amount_eur": request.amount_eur}


@app.post("/bookings/{reference}/cancel")
def cancel_booking(reference: str) -> dict:
    with database() as connection:
        segments = connection.execute("""SELECT bs.* FROM booking_segments bs JOIN bookings b ON b.id = bs.booking_id WHERE b.reference = ? AND bs.status = 'confirmed'""", (reference.upper(),)).fetchall()
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
def reschedule_booking(reference: str, request: RescheduleInput) -> dict:
    with database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        segment = connection.execute("""SELECT bs.*, f.origin_iata, f.destination_iata FROM booking_segments bs JOIN bookings b ON b.id = bs.booking_id JOIN flights f ON f.id = bs.flight_id WHERE b.reference = ? AND bs.id = ? AND bs.status = 'confirmed'""", (reference.upper(), request.booking_segment_id)).fetchone()
        if not segment:
            raise HTTPException(404, "Confirmed booking segment not found.")
        if segment["fare_tier"] == "economy_light":
            raise HTTPException(409, "Economy Light flights cannot be changed.")
        replacement = connection.execute("SELECT * FROM flights WHERE id = ? AND status = 'scheduled'", (request.new_flight_id,)).fetchone()
        if not replacement:
            raise HTTPException(404, "Replacement scheduled flight not found.")
        if (replacement["origin_iata"], replacement["destination_iata"]) != (segment["origin_iata"], segment["destination_iata"]):
            raise HTTPException(409, "Voluntary changes must keep the same origin and destination.")
        seat = seat_for(connection, replacement["id"], segment["cabin"], request.seat_number)
        _, multiplier, replacement_fare, benefit = price_for(connection, replacement["id"], segment["fare_tier"], "gold" if segment["loyalty_benefit"] else "none")
        change_fee = 45 if segment["fare_tier"] == "economy_classic" else 0
        total_difference = replacement_fare - segment["fare_amount_eur"] + change_fee
        connection.execute("""UPDATE booking_segments SET flight_id = ?, seat_id = ?, fare_amount_eur = ?, applied_price_multiplier = ?, loyalty_benefit = ?, status = 'confirmed' WHERE id = ?""", (replacement["id"], seat["id"], replacement_fare, multiplier, benefit, segment["id"]))
        connection.execute("UPDATE bookings SET total_amount_eur = MAX(0, total_amount_eur + ?), updated_at_utc = CURRENT_TIMESTAMP WHERE reference = ?", (total_difference, reference.upper()))
        return {"booking_reference": reference.upper(), "booking_segment_id": segment["id"], "new_flight_id": replacement["id"], "seat_number": seat["seat_number"], "change_fee_eur": change_fee, "fare_difference_eur": replacement_fare - segment["fare_amount_eur"], "amount_due_eur": max(0, total_difference)}
