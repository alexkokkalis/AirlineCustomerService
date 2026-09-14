"""Create the reproducible Ionian Airlines SQLite database and future-flight seed data."""

from __future__ import annotations

import sqlite3
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATABASE_PATH = PROJECT_ROOT / "data" / "ionian_airlines.db"
SCHEMA_PATH = PROJECT_ROOT / "database" / "schema.sql"

FARE_MULTIPLIERS = {
    "economy_light": 1.0,
    "economy_classic": 1.2,
    "economy_plus": 1.55,
    "business": 3.2,
}

AIRPORTS = [
    ("ATH", "Athens International Airport", "Athens", "Greece", "Europe/Athens"),
    ("SKG", "Thessaloniki Airport", "Thessaloniki", "Greece", "Europe/Athens"),
    ("JTR", "Santorini Airport", "Santorini", "Greece", "Europe/Athens"),
    ("JMK", "Mykonos Airport", "Mykonos", "Greece", "Europe/Athens"),
    ("HER", "Heraklion International Airport", "Heraklion", "Greece", "Europe/Athens"),
    ("LHR", "London Heathrow Airport", "London", "United Kingdom", "Europe/London"),
    ("BER", "Berlin Brandenburg Airport", "Berlin", "Germany", "Europe/Berlin"),
    ("JFK", "John F. Kennedy International Airport", "New York", "United States", "America/New_York"),
    ("MEL", "Melbourne Airport", "Melbourne", "Australia", "Australia/Melbourne"),
]

AIRCRAFT = [
    ("A320", "Airbus A320neo", 150, 12),
    ("A321", "Airbus A321neo", 180, 16),
    ("A350", "Airbus A350-900", 270, 40),
]

# Every timestamp is UTC. This is a deliberately small but varied future schedule
# for the week beginning 14 September 2026.
FLIGHTS = [
    ("IO101", "A320", "ATH", "SKG", "2026-09-14T04:15:00Z", "2026-09-14T05:20:00Z", "A03", 65),
    ("IO103", "A320", "ATH", "SKG", "2026-09-14T15:30:00Z", "2026-09-14T16:35:00Z", "A07", 72),
    ("IO105", "A320", "ATH", "SKG", "2026-09-16T04:15:00Z", "2026-09-16T05:20:00Z", "A03", 59),
    ("IO107", "A320", "ATH", "SKG", "2026-09-18T15:30:00Z", "2026-09-18T16:35:00Z", "A07", 80),
    ("IO102", "A320", "SKG", "ATH", "2026-09-14T06:10:00Z", "2026-09-14T07:15:00Z", "B02", 63),
    ("IO104", "A320", "SKG", "ATH", "2026-09-14T17:20:00Z", "2026-09-14T18:25:00Z", "B04", 76),
    ("IO106", "A320", "SKG", "ATH", "2026-09-16T06:10:00Z", "2026-09-16T07:15:00Z", "B02", 60),
    ("IO108", "A320", "SKG", "ATH", "2026-09-18T17:20:00Z", "2026-09-18T18:25:00Z", "B04", 82),
    ("IO201", "A321", "ATH", "JTR", "2026-09-15T07:10:00Z", "2026-09-15T07:55:00Z", "C12", 88),
    ("IO203", "A321", "ATH", "JTR", "2026-09-18T12:15:00Z", "2026-09-18T13:00:00Z", "C14", 105),
    ("IO202", "A321", "JTR", "ATH", "2026-09-15T08:40:00Z", "2026-09-15T09:25:00Z", "D05", 84),
    ("IO204", "A321", "JTR", "ATH", "2026-09-18T13:45:00Z", "2026-09-18T14:30:00Z", "D08", 110),
    ("IO301", "A320", "ATH", "JMK", "2026-09-16T10:20:00Z", "2026-09-16T11:00:00Z", "C03", 95),
    ("IO303", "A320", "ATH", "JMK", "2026-09-19T05:40:00Z", "2026-09-19T06:20:00Z", "C09", 82),
    ("IO302", "A320", "JMK", "ATH", "2026-09-16T11:45:00Z", "2026-09-16T12:25:00Z", "D03", 91),
    ("IO304", "A320", "JMK", "ATH", "2026-09-19T07:05:00Z", "2026-09-19T07:45:00Z", "D09", 86),
    ("IO401", "A321", "ATH", "HER", "2026-09-14T06:30:00Z", "2026-09-14T07:20:00Z", "C18", 78),
    ("IO403", "A321", "ATH", "HER", "2026-09-17T16:15:00Z", "2026-09-17T17:05:00Z", "C21", 90),
    ("IO402", "A321", "HER", "ATH", "2026-09-14T08:05:00Z", "2026-09-14T08:55:00Z", "D14", 75),
    ("IO404", "A321", "HER", "ATH", "2026-09-17T17:50:00Z", "2026-09-17T18:40:00Z", "D16", 94),
    ("IO501", "A321", "ATH", "LHR", "2026-09-14T05:50:00Z", "2026-09-14T09:25:00Z", "E11", 175),
    ("IO503", "A321", "ATH", "LHR", "2026-09-16T14:40:00Z", "2026-09-16T18:15:00Z", "E16", 155),
    ("IO505", "A321", "ATH", "LHR", "2026-09-18T16:25:00Z", "2026-09-18T20:00:00Z", "E19", 225),
    ("IO502", "A321", "LHR", "ATH", "2026-09-15T08:30:00Z", "2026-09-15T12:00:00Z", "F02", 170),
    ("IO504", "A321", "LHR", "ATH", "2026-09-17T17:15:00Z", "2026-09-17T20:45:00Z", "F05", 160),
    ("IO506", "A321", "LHR", "ATH", "2026-09-19T18:45:00Z", "2026-09-19T22:15:00Z", "F08", 230),
    ("IO601", "A321", "ATH", "BER", "2026-09-15T13:40:00Z", "2026-09-15T16:20:00Z", "E05", 135),
    ("IO603", "A321", "ATH", "BER", "2026-09-19T08:25:00Z", "2026-09-19T11:05:00Z", "E07", 155),
    ("IO602", "A321", "BER", "ATH", "2026-09-16T17:15:00Z", "2026-09-16T19:50:00Z", "F10", 130),
    ("IO604", "A321", "BER", "ATH", "2026-09-20T12:40:00Z", "2026-09-20T15:15:00Z", "F12", 145),
    ("IO701", "A350", "ATH", "JFK", "2026-09-16T09:00:00Z", "2026-09-16T18:20:00Z", "G01", 620),
    ("IO702", "A350", "JFK", "ATH", "2026-09-18T20:10:00Z", "2026-09-19T06:10:00Z", "H02", 650),
    ("IO801", "A350", "ATH", "MEL", "2026-09-17T07:30:00Z", "2026-09-18T02:30:00Z", "G06", 1050),
    ("IO802", "A350", "MEL", "ATH", "2026-09-19T04:15:00Z", "2026-09-19T21:20:00Z", "H07", 1080)
]


def create_seats(connection: sqlite3.Connection, flight_id: int, aircraft_code: str) -> None:
    """Seed a simple, consistent seat map for each flight."""
    business_rows, economy_start, economy_end = {
        "A320": (range(1, 4), 4, 28),
        "A321": (range(1, 5), 5, 34),
        "A350": (range(1, 11), 11, 55),
    }[aircraft_code]

    business_seats = [
        (flight_id, f"{row}{letter}", "business", "business")
        for row in business_rows
        for letter in "ACDF"
    ]
    economy_seats = []
    for row in range(economy_start, economy_end + 1):
        for letter in "ABCDEF":
            seat_type = "extra_legroom" if row == economy_start else "preferred" if row in {economy_start + 1, economy_end} else "standard"
            economy_seats.append((flight_id, f"{row}{letter}", "economy", seat_type))

    connection.executemany(
        "INSERT INTO seats (flight_id, seat_number, cabin, seat_type) VALUES (?, ?, ?, ?)",
        business_seats + economy_seats,
    )


def create_fares(connection: sqlite3.Connection, flight_id: int, economy_light_price: int) -> None:
    fares = [
        ("economy_light", "economy", round(economy_light_price * FARE_MULTIPLIERS["economy_light"])),
        ("economy_classic", "economy", round(economy_light_price * FARE_MULTIPLIERS["economy_classic"])),
        ("economy_plus", "economy", round(economy_light_price * FARE_MULTIPLIERS["economy_plus"])),
        ("business", "business", round(economy_light_price * FARE_MULTIPLIERS["business"])),
    ]
    connection.executemany(
        "INSERT INTO flight_fares (flight_id, fare_tier, cabin, base_price_eur) VALUES (?, ?, ?, ?)",
        [(flight_id, tier, cabin, price) for tier, cabin, price in fares],
    )


def initialise_database() -> None:
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()

    with sqlite3.connect(DATABASE_PATH) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(SCHEMA_PATH.read_text())
        connection.executemany(
            "INSERT INTO airports (iata_code, name, city, country, timezone) VALUES (?, ?, ?, ?, ?)",
            AIRPORTS,
        )
        connection.executemany(
            "INSERT INTO aircraft (code, model, economy_capacity, business_capacity) VALUES (?, ?, ?, ?)",
            AIRCRAFT,
        )

        aircraft_ids = dict(connection.execute("SELECT code, id FROM aircraft"))
        for flight_number, aircraft_code, origin, destination, departure, arrival, gate, light_price in FLIGHTS:
            cursor = connection.execute(
                """
                INSERT INTO flights (
                    flight_number, aircraft_id, origin_iata, destination_iata,
                    scheduled_departure_at_utc, scheduled_arrival_at_utc, status, gate
                ) VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?)
                """,
                (flight_number, aircraft_ids[aircraft_code], origin, destination, departure, arrival, gate),
            )
            flight_id = cursor.lastrowid
            create_seats(connection, flight_id, aircraft_code)
            create_fares(connection, flight_id, light_price)
            connection.execute(
                "INSERT INTO flight_status_history (flight_id, new_status, reason) VALUES (?, 'scheduled', 'Initial future-flight schedule')",
                (flight_id,),
            )

    print(f"Created {DATABASE_PATH}")
    print(f"Seeded {len(FLIGHTS)} future flights across {len(AIRPORTS)} airports.")


if __name__ == "__main__":
    initialise_database()
