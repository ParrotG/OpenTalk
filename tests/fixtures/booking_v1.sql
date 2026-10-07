PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS slots (
    slot_id TEXT PRIMARY KEY,
    room TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    CHECK (starts_at < ends_at),
    UNIQUE (room, starts_at, ends_at)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('book', 'cancel')),
    target_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    supersedes TEXT REFERENCES operations(operation_id),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'succeeded', 'failed', 'invalidated')
    ),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bookings (
    booking_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    slot_id TEXT NOT NULL REFERENCES slots(slot_id),
    operation_id TEXT NOT NULL UNIQUE REFERENCES operations(operation_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'cancelled')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_slot
    ON bookings(slot_id) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    session_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (operation_id, event_type)
);
