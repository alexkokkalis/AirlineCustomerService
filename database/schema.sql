PRAGMA foreign_keys = ON;

CREATE TABLE airports (
    iata_code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    country TEXT NOT NULL,
    timezone TEXT NOT NULL
);

CREATE TABLE aircraft (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    model TEXT NOT NULL,
    economy_capacity INTEGER NOT NULL CHECK (economy_capacity >= 0),
    business_capacity INTEGER NOT NULL CHECK (business_capacity >= 0)
);

CREATE TABLE flights (
    id INTEGER PRIMARY KEY,
    flight_number TEXT NOT NULL,
    aircraft_id INTEGER NOT NULL REFERENCES aircraft(id),
    origin_iata TEXT NOT NULL REFERENCES airports(iata_code),
    destination_iata TEXT NOT NULL REFERENCES airports(iata_code),
    scheduled_departure_at_utc TEXT NOT NULL,
    scheduled_arrival_at_utc TEXT NOT NULL,
    actual_departure_at_utc TEXT,
    actual_arrival_at_utc TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled' CHECK (status IN ('scheduled', 'boarding', 'final_call', 'delayed', 'gate_change', 'cancelled', 'departed', 'arrived')),
    gate TEXT,
    disruption_reason TEXT,
    CHECK (origin_iata <> destination_iata),
    UNIQUE (flight_number, scheduled_departure_at_utc)
);

CREATE TABLE seats (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    seat_number TEXT NOT NULL,
    cabin TEXT NOT NULL CHECK (cabin IN ('economy', 'business')),
    seat_type TEXT NOT NULL CHECK (seat_type IN ('standard', 'preferred', 'extra_legroom', 'business')),
    UNIQUE (flight_id, seat_number)
);

CREATE TABLE flight_fares (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    fare_tier TEXT NOT NULL CHECK (fare_tier IN ('economy_light', 'economy_classic', 'economy_plus', 'business')),
    cabin TEXT NOT NULL CHECK (cabin IN ('economy', 'business')),
    base_price_eur INTEGER NOT NULL CHECK (base_price_eur > 0),
    is_available INTEGER NOT NULL DEFAULT 1 CHECK (is_available IN (0, 1)),
    UNIQUE (flight_id, fare_tier)
);

CREATE TABLE customers (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    email TEXT,
    phone TEXT,
    loyalty_number TEXT UNIQUE,
    loyalty_tier TEXT NOT NULL DEFAULT 'none' CHECK (loyalty_tier IN ('none', 'bronze', 'silver', 'gold')),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE bookings (
    id INTEGER PRIMARY KEY,
    reference TEXT NOT NULL UNIQUE,
    primary_contact_customer_id INTEGER NOT NULL REFERENCES customers(id),
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled', 'completed')),
    currency TEXT NOT NULL DEFAULT 'EUR',
    total_amount_eur INTEGER NOT NULL DEFAULT 0 CHECK (total_amount_eur >= 0),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE booking_segments (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id) ON DELETE CASCADE,
    customer_id INTEGER NOT NULL REFERENCES customers(id),
    flight_id INTEGER NOT NULL REFERENCES flights(id),
    seat_id INTEGER UNIQUE REFERENCES seats(id),
    fare_tier TEXT NOT NULL CHECK (fare_tier IN ('economy_light', 'economy_classic', 'economy_plus', 'business')),
    cabin TEXT NOT NULL CHECK (cabin IN ('economy', 'business')),
    fare_amount_eur INTEGER NOT NULL CHECK (fare_amount_eur >= 0),
    standard_price_multiplier REAL NOT NULL,
    applied_price_multiplier REAL NOT NULL,
    loyalty_benefit TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled', 'rescheduled', 'flown', 'no_show')),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (booking_id, customer_id, flight_id)
);

CREATE TABLE ancillaries (
    id INTEGER PRIMARY KEY,
    booking_segment_id INTEGER NOT NULL REFERENCES booking_segments(id) ON DELETE CASCADE,
    ancillary_type TEXT NOT NULL CHECK (ancillary_type IN ('checked_bag', 'special_item', 'pet', 'seat_selection')),
    description TEXT NOT NULL,
    amount_eur INTEGER NOT NULL CHECK (amount_eur >= 0),
    status TEXT NOT NULL DEFAULT 'confirmed' CHECK (status IN ('confirmed', 'cancelled')),
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE assistance_requests (
    id INTEGER PRIMARY KEY,
    booking_segment_id INTEGER NOT NULL REFERENCES booking_segments(id) ON DELETE CASCADE,
    assistance_type TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'confirmed', 'cancelled')),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE flight_status_history (
    id INTEGER PRIMARY KEY,
    flight_id INTEGER NOT NULL REFERENCES flights(id) ON DELETE CASCADE,
    old_status TEXT,
    new_status TEXT NOT NULL,
    reason TEXT,
    recorded_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE refunds (
    id INTEGER PRIMARY KEY,
    booking_segment_id INTEGER NOT NULL REFERENCES booking_segments(id),
    refund_type TEXT NOT NULL CHECK (refund_type IN ('voluntary_refund', 'travel_credit', 'disruption_refund', 'compensation')),
    amount_eur INTEGER NOT NULL CHECK (amount_eur >= 0),
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'paid')),
    created_at_utc TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_flights_search ON flights (destination_iata, scheduled_departure_at_utc, status);
CREATE INDEX idx_booking_segments_booking ON booking_segments (booking_id);
CREATE INDEX idx_booking_segments_customer ON booking_segments (customer_id);
CREATE INDEX idx_ancillaries_segment ON ancillaries (booking_segment_id);
