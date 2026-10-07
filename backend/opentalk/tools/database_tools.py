"""Read-only SQL and transactional room/time reservation edits for the local demo."""

import asyncio
import hashlib
import json
import sqlite3
import time
import tomllib
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from uuid import NAMESPACE_URL, uuid4, uuid5

from opentalk.config import PROJECT_ROOT
from opentalk.domain.models import Booking, BookingError, Operation, Slot


class DatabaseTools:
    def __init__(self, service, rooms=()):
        self.service = service
        self.rooms = tuple(rooms)
        with (PROJECT_ROOT / "config/booking_agent.toml").open("rb") as file:
            options = tomllib.load(file)["query"]
        self.max_rows = options["max_rows"]
        self.max_vm_steps = options["max_vm_steps"]
        self.query_timeout = options["timeout_seconds"]
        if (type(self.max_rows) is not int or self.max_rows < 1
                or type(self.max_vm_steps) is not int or self.max_vm_steps < 1
                or type(self.query_timeout) not in (float, int) or not 0 < self.query_timeout <= 60):
            raise ValueError("Invalid query resource limits.")

    async def query(self, sql):
        try:
            return await asyncio.to_thread(self._query, sql)
        except (sqlite3.Error, ValueError) as error:
            return {"ok": False, "error": "query_rejected", "message": str(error)}

    def _query(self, sql):
        if not isinstance(sql, str) or not sql.strip():
            raise ValueError("Provide one read-only SQL statement.")
        path = self.service.repository.database_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(path, uri=True, timeout=self.query_timeout)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            allowed = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}

            def authorize(action, first, second, database, source):
                if action not in allowed:
                    return sqlite3.SQLITE_DENY
                if action == sqlite3.SQLITE_FUNCTION and (second or "").lower() in {
                    "load_extension", "readfile", "writefile",
                }:
                    return sqlite3.SQLITE_DENY
                return sqlite3.SQLITE_OK
            connection.set_authorizer(authorize)
            deadline = time.monotonic() + self.query_timeout
            steps = 0

            def budget():
                nonlocal steps
                steps += 1000
                return int(steps >= self.max_vm_steps or time.monotonic() >= deadline)
            connection.set_progress_handler(budget, 1000)
            cursor = connection.execute(sql)
            if cursor.description is None:
                raise ValueError("Only result-producing read queries are permitted.")
            rows = cursor.fetchmany(self.max_rows + 1)
            return {"ok": True, "columns": [item[0] for item in cursor.description],
                    "rows": [dict(row) for row in rows[:self.max_rows]],
                    "truncated": len(rows) > self.max_rows, "max_rows": self.max_rows}

    def _time(self, value):
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=self.service.timezone)
            return self.service._utc(parsed)
        except (TypeError, ValueError) as error:
            raise BookingError("invalid_time", "Provide valid ISO date-time values.") from error

    def _interval(self, room, starts_at, ends_at):
        if not isinstance(room, str) or not room.strip():
            raise BookingError("invalid_room", "A non-empty room name is required.")
        start, end = self._time(starts_at), self._time(ends_at)
        if start >= end:
            raise BookingError("invalid_time", "The interval must end after it starts.")
        return room.strip(), start, end

    async def edit(self, action, room, starts_at, ends_at, *, new_room=None,
                   new_starts_at=None, new_ends_at=None, request_id):
        try:
            return await asyncio.to_thread(self._edit, action, room, starts_at, ends_at,
                                           new_room, new_starts_at, new_ends_at, request_id)
        except BookingError as error:
            return {"ok": False, "error": error.code, "message": str(error)}
        except sqlite3.Error:
            return {"ok": False, "error": "database_error", "message": "The database edit failed."}

    def _edit(self, action, room, starts_at, ends_at, new_room, new_starts_at, new_ends_at, request_id):
        if action not in {"add", "delete", "update"}:
            raise BookingError("invalid_action", "Use add, delete, or update.")
        original = self._interval(room, starts_at, ends_at)
        target = self._interval(new_room if new_room is not None else original[0],
                                new_starts_at if new_starts_at is not None else original[1],
                                new_ends_at if new_ends_at is not None else original[2])
        if action != "update" and target != original:
            raise BookingError("invalid_input", "Replacement values apply only to update.")
        if not isinstance(request_id, str) or not request_id:
            raise BookingError("invalid_input", "A host-generated request ID is required.")
        payload = {"action": action, "original": original, "target": target}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        operation_id = str(uuid5(NAMESPACE_URL, f"agent-edit:{self.service.session_id}:{request_id}:{digest}"))
        now = self.service._utc(self.service.clock())
        repository = self.service.repository
        with repository.transaction() as connection:
            existing = repository.find_operation(connection, operation_id)
            if existing is not None:
                return {"ok": True, "action": action, "operation_id": operation_id,
                        "booking": asdict(existing.result), "replayed": True}
            booking = None
            if action in {"delete", "update"}:
                rows = connection.execute(
                    """SELECT b.booking_id FROM bookings b JOIN slots s ON s.slot_id = b.slot_id
                    WHERE b.status = 'active' AND s.room = ?
                    AND julianday(s.starts_at) = julianday(?) AND julianday(s.ends_at) = julianday(?)""", original,
                ).fetchall()
                if len(rows) != 1:
                    raise BookingError("reservation_not_found", "No unique active reservation matches this room and interval.")
                booking = repository.find_booking(connection, rows[0]["booking_id"])
            if action in {"add", "update"}:
                conflict = connection.execute(
                    """SELECT 1 FROM bookings b JOIN slots s ON s.slot_id = b.slot_id
                    WHERE b.status = 'active' AND s.room = ? AND b.booking_id != ?
                    AND julianday(s.starts_at) < julianday(?) AND julianday(s.ends_at) > julianday(?)""",
                    (target[0], booking.booking_id if booking else "", target[2], target[1]),
                ).fetchone()
                if conflict:
                    raise BookingError("slot_unavailable", "The interval overlaps an active reservation.")
                slot_id = str(uuid5(NAMESPACE_URL, f"opentalk:slot:{target[0]}:{target[1]}:{target[2]}"))
                slot = repository.find_slot(connection, slot_id)
                if slot is None:
                    slot = Slot(slot_id, *target)
                    repository.insert_slot(connection, slot)
            else:
                slot_id = booking.slot_id
            operation = Operation(operation_id, self.service.user_id, self.service.session_id,
                                  "cancel" if action == "delete" else "book", slot_id, 1,
                                  "pending", None, None)
            repository.insert_operation(connection, operation, now)
            if action == "add":
                booking = Booking(str(uuid4()), self.service.user_id, slot_id, "active", now)
                repository.insert_booking(connection, booking, operation_id)
            elif action == "delete":
                repository.cancel_booking(connection, booking.booking_id)
                booking = repository.find_booking(connection, booking.booking_id)
            else:
                connection.execute("UPDATE bookings SET slot_id = ? WHERE booking_id = ?", (slot_id, booking.booking_id))
                booking = repository.find_booking(connection, booking.booking_id)
            repository.update_operation(connection, operation_id, "succeeded", now, result=booking)
            repository.record_event(connection, operation, "agent_edit", now,
                                    {**payload, "booking": asdict(booking)})
            return {"ok": True, "action": action, "operation_id": operation_id,
                    "booking": asdict(booking), "replayed": False}
