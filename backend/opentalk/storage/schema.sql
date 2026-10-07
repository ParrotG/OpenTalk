PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    uid TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    department TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resources (
    rid TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    type TEXT NOT NULL,
    location TEXT NOT NULL,
    capacity INTEGER NOT NULL CHECK (typeof(capacity) = 'integer' AND capacity > 0),
    metadata TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata) AND json_type(metadata) = 'object')
);
CREATE TABLE IF NOT EXISTS slots (
    sid TEXT PRIMARY KEY,
    rid TEXT NOT NULL REFERENCES resources(rid),
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    CHECK (starts_at < ends_at),
    UNIQUE (rid, starts_at, ends_at)
);
CREATE INDEX IF NOT EXISTS slots_by_resource_time ON slots(rid, starts_at, ends_at);
CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    uid TEXT NOT NULL REFERENCES users(uid),
    kind TEXT NOT NULL CHECK (kind IN ('book', 'cancel', 'add', 'delete', 'update')),
    target_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    supersedes TEXT REFERENCES operations(operation_id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'invalidated')),
    request_json TEXT CHECK (request_json IS NULL OR json_valid(request_json)),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS slot_users (
    booking_id TEXT PRIMARY KEY,
    sid TEXT NOT NULL REFERENCES slots(sid),
    uid TEXT NOT NULL REFERENCES users(uid),
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    status TEXT NOT NULL CHECK (status IN ('active', 'cancelled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_booking_per_user_slot
    ON slot_users(sid, uid) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS active_bookings_by_slot ON slot_users(sid) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS bookings_by_user ON slot_users(uid, status);

CREATE VIEW IF NOT EXISTS reservations AS
SELECT b.booking_id, b.uid, u.name AS user_name, u.department,
       s.sid, s.rid, r.name AS resource_name, r.type, r.location, r.capacity,
       s.starts_at, s.ends_at, b.status, b.created_at, b.updated_at
FROM slot_users b JOIN users u USING(uid) JOIN slots s USING(sid) JOIN resources r USING(rid);

CREATE VIEW IF NOT EXISTS slot_availability AS
WITH points AS (
    SELECT sid, starts_at AS at FROM slots
    UNION
    SELECT candidate.sid, occupied.starts_at
    FROM slots candidate JOIN slots occupied ON candidate.rid = occupied.rid
    JOIN slot_users b ON b.sid = occupied.sid AND b.status = 'active'
    WHERE occupied.starts_at > candidate.starts_at AND occupied.starts_at < candidate.ends_at
), occupancy AS (
    SELECT p.sid, p.at, count(b.booking_id) AS occupied
    FROM points p JOIN slots candidate ON candidate.sid = p.sid
    LEFT JOIN slots occupied ON occupied.rid = candidate.rid
         AND occupied.starts_at <= p.at AND occupied.ends_at > p.at
    LEFT JOIN slot_users b ON b.sid = occupied.sid AND b.status = 'active'
    GROUP BY p.sid, p.at
)
SELECT s.sid, s.rid, r.name AS resource_name, r.type, r.location,
       s.starts_at, s.ends_at, r.capacity, max(o.occupied) AS peak_occupancy,
       r.capacity - max(o.occupied) AS remaining_capacity
FROM slots s JOIN resources r USING(rid) JOIN occupancy o USING(sid)
GROUP BY s.sid;
