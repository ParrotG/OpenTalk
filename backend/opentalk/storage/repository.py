"""SQLite access and transaction boundaries for the booking service."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from opentalk.domain.models import Booking, Operation, Slot


class BookingRepository:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.executescript(
                Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
            )

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self):
        """Serialize writes and commit business state and audit events together."""
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def find_operation(self, connection, operation_id: str) -> Operation | None:
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
        ).fetchone()
        if row is None:
            return None
        result = Booking(**json.loads(row["result_json"])) if row["result_json"] else None
        return Operation(
            **{key: row[key] for key in (
                "operation_id", "user_id", "session_id", "kind", "target_id",
                "version", "status", "error_code", "supersedes",
            )},
            result=result,
        )

    def find_slot(self, connection, slot_id: str) -> Slot | None:
        row = connection.execute(
            "SELECT * FROM slots WHERE slot_id = ?", (slot_id,)
        ).fetchone()
        return Slot(**dict(row)) if row else None

    def insert_slot(self, connection, slot: Slot) -> None:
        connection.execute(
            "INSERT INTO slots VALUES (?, ?, ?, ?)",
            (slot.slot_id, slot.room, slot.starts_at, slot.ends_at),
        )

    def overlapping_slot(self, connection, slot: Slot) -> bool:
        return connection.execute(
            "SELECT 1 FROM slots WHERE room = ? AND starts_at < ? AND ends_at > ?",
            (slot.room, slot.ends_at, slot.starts_at),
        ).fetchone() is not None

    def available_slots(self, connection) -> list[Slot]:
        rows = connection.execute(
            """SELECT s.* FROM slots s WHERE NOT EXISTS (
                SELECT 1 FROM bookings b WHERE b.slot_id = s.slot_id AND b.status = 'active'
            ) ORDER BY s.starts_at, s.room"""
        ).fetchall()
        return [Slot(**dict(row)) for row in rows]

    def occupied(self, connection, slot_id: str) -> bool:
        return connection.execute(
            "SELECT 1 FROM bookings WHERE slot_id = ? AND status = 'active'", (slot_id,)
        ).fetchone() is not None

    def insert_operation(self, connection, operation: Operation, now: str) -> None:
        connection.execute(
            """INSERT INTO operations
            (operation_id, user_id, session_id, kind, target_id, version, supersedes, status,
             result_json, error_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
            (operation.operation_id, operation.user_id, operation.session_id,
             operation.kind, operation.target_id, operation.version, operation.supersedes,
             operation.status, now, now),
        )

    def update_operation(
        self, connection, operation_id: str, status: str, now: str,
        result: Booking | None = None, error_code: str | None = None,
    ) -> None:
        connection.execute(
            """UPDATE operations SET status = ?, result_json = ?, error_code = ?,
            updated_at = ? WHERE operation_id = ?""",
            (status, json.dumps(asdict(result)) if result else None,
             error_code, now, operation_id),
        )

    def insert_booking(self, connection, booking: Booking, operation_id: str) -> None:
        connection.execute(
            """INSERT INTO bookings
            (booking_id, user_id, slot_id, operation_id, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (booking.booking_id, booking.user_id, booking.slot_id,
             operation_id, booking.status, booking.created_at),
        )

    def find_booking(self, connection, booking_id: str) -> Booking | None:
        row = connection.execute(
            """SELECT booking_id, user_id, slot_id, status, created_at
            FROM bookings WHERE booking_id = ?""", (booking_id,)
        ).fetchone()
        return Booking(**dict(row)) if row else None

    def cancel_booking(self, connection, booking_id: str) -> None:
        connection.execute(
            "UPDATE bookings SET status = 'cancelled' WHERE booking_id = ?", (booking_id,)
        )

    def record_event(self, connection, operation: Operation, event_type: str,
                     now: str, payload: dict) -> None:
        event_id = str(uuid5(NAMESPACE_URL, f"opentalk:{operation.operation_id}:{event_type}"))
        connection.execute(
            """INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING""",
            (event_id, operation.operation_id, operation.session_id, event_type,
             json.dumps(payload, ensure_ascii=False, sort_keys=True), now),
        )

    def events(self, connection, session_id: str, user_id: str) -> list[dict]:
        rows = connection.execute(
            """SELECT e.* FROM events e JOIN operations o
            ON o.operation_id = e.operation_id
            WHERE e.session_id = ? AND o.user_id = ? ORDER BY e.rowid""",
            (session_id, user_id),
        ).fetchall()
        return [
            {**{key: row[key] for key in row.keys() if key != "payload_json"},
             "payload": json.loads(row["payload_json"])}
            for row in rows
        ]
