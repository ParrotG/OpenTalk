"""User-scoped resource bookings with interval capacity and durable business operations."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo

from opentalk.domain.models import Booking, BookingError, Operation, Resource, Slot, User
from opentalk.storage.repository import BookingRepository, stable_id


class BookingService:
    def __init__(self, repository: BookingRepository, user_id="demo-user",
                 timezone="Asia/Singapore", clock: Callable[[], datetime] | None = None):
        self.repository = repository
        self.user_id = self._identifier(user_id)
        self.timezone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(UTC))
        with repository.transaction() as connection:
            repository.insert_user(connection, User(self.user_id, self.user_id, "Unspecified"))

    @staticmethod
    def _identifier(value):
        if not isinstance(value, str) or not value.strip():
            raise BookingError("invalid_input", "Identifiers must be non-empty strings.")
        return value.strip()

    @staticmethod
    def _utc(value):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BookingError("invalid_time", "A timezone-aware timestamp is required.")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _time(self, value):
        try:
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(parsed, datetime):
                raise ValueError("A timestamp is required.")
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=self.timezone)
            return self._utc(parsed)
        except (TypeError, ValueError) as error:
            raise BookingError("invalid_time", "Provide valid ISO date-time values.") from error

    def _interval(self, starts_at, ends_at):
        start, end = self._time(starts_at), self._time(ends_at)
        if start >= end:
            raise BookingError("invalid_time", "The interval must end after it starts.")
        return start, end

    def _resource(self, connection, reference):
        resource = self.repository.find_resource(connection, self._identifier(reference))
        if resource is None:
            raise BookingError("resource_not_found", "No unique resource matches this name or ID.")
        return resource

    def seed_resource(self, name, *, capacity=1, resource_type="meeting_room", location="Unspecified", metadata=None):
        name = self._identifier(name)
        metadata = {} if metadata is None else metadata
        if type(capacity) is not int or capacity < 1 or not isinstance(metadata, dict):
            raise BookingError("invalid_resource", "Capacity must be a positive integer and metadata an object.")
        resource = Resource(stable_id("resource", name), name, self._identifier(resource_type),
                            self._identifier(location), capacity, metadata)
        with self.repository.transaction() as connection:
            existing = self.repository.find_resource(connection, name)
            if existing:
                return existing
            self.repository.insert_resource(connection, resource)
        return resource

    def _slot(self, connection, resource, start, end):
        row = connection.execute("SELECT sid FROM slots WHERE rid=? AND starts_at=? AND ends_at=?",
                                 (resource.rid, start, end)).fetchone()
        if row:
            return self.repository.find_slot(connection, row[0])
        slot = Slot(stable_id("slot", f"{resource.name}:{start}:{end}"), resource.name, start, end, resource.rid)
        self.repository.insert_slot(connection, slot)
        return slot

    def seed_slot(self, room, starts_at, ends_at):
        """Provision a discoverable interval; overlapping intervals do not consume capacity."""
        start, end = self._utc(starts_at), self._utc(ends_at)
        if start >= end:
            raise BookingError("invalid_time", "The slot must end after it starts.")
        resource = self.seed_resource(room)
        with self.repository.transaction() as connection:
            return self._slot(connection, resource, start, end)

    def _check_capacity(self, connection, resource, start, end, *, excluding=""):
        rows = connection.execute("""SELECT b.uid, s.starts_at, s.ends_at FROM slot_users b
            JOIN slots s USING(sid) WHERE b.status='active' AND s.rid=? AND b.booking_id!=?
            AND s.starts_at<? AND s.ends_at>?""", (resource.rid, excluding, end, start)).fetchall()
        if any(row["uid"] == self.user_id for row in rows):
            raise BookingError("slot_unavailable", "You already have an overlapping booking for this resource.")
        events = []
        for row in rows:
            events.extend(((max(start, row["starts_at"]), 1), (min(end, row["ends_at"]), -1)))
        count = 0
        # Half-open intervals allow one reservation to start exactly when another ends.
        for _, change in sorted(events):
            count += change
            if count >= resource.capacity:
                raise BookingError("slot_unavailable", "The resource has no remaining capacity during this interval.")

    def _owned_operation(self, connection, operation_id):
        operation = self.repository.find_operation(connection, operation_id)
        if operation is None or operation.user_id != self.user_id:
            raise BookingError("operation_not_found", "The operation was not found for this user.")
        return operation

    def _owned_booking(self, connection, booking_id):
        booking = self.repository.find_booking(connection, booking_id)
        if booking is None or booking.user_id != self.user_id:
            raise BookingError("booking_not_found", "The booking was not found for this user.")
        return booking

    def get_operation(self, operation_id):
        with self.repository.connection() as connection:
            return self._owned_operation(connection, operation_id)

    def get_booking(self, booking_id):
        with self.repository.connection() as connection:
            return self._owned_booking(connection, booking_id)

    def list_operations(self):
        with self.repository.connection() as connection:
            return self.repository.operations(connection, self.user_id)

    def edit(self, action, resource, starts_at, ends_at, *, new_resource=None,
             new_starts_at=None, new_ends_at=None, request_id, uid=None):
        """Atomically edit only the fixed caller's booking; caller identity is never inferred from SQL."""
        if uid is not None and uid != self.user_id:
            raise BookingError("forbidden", "You can only create or change your own bookings.")
        if action not in {"add", "delete", "update"}:
            raise BookingError("invalid_action", "Use add, delete, or update.")
        self._identifier(request_id)
        start, end = self._interval(starts_at, ends_at)
        target_start, target_end = self._interval(new_starts_at if new_starts_at is not None else start,
                                                 new_ends_at if new_ends_at is not None else end)
        now = self._utc(self.clock())
        operation_id = stable_id("edit", f"{self.user_id}:{request_id}")
        failure = None
        with self.repository.transaction() as connection:
            original_resource = self._resource(connection, resource)
            target_resource = self._resource(connection, new_resource) if new_resource is not None else original_resource
            payload = {"action": action, "original": [original_resource.rid, start, end],
                       "target": [target_resource.rid, target_start, target_end]}
            if action != "update" and payload["original"] != payload["target"]:
                raise BookingError("invalid_input", "Replacement values apply only to update.")
            existing = self.repository.find_operation(connection, operation_id)
            if existing:
                if existing.request != payload:
                    raise BookingError("idempotency_conflict", "The request ID belongs to different edit arguments.")
                if existing.status == "failed":
                    raise BookingError(existing.error_code, "The edit operation previously failed.")
                return {"ok": True, "action": action, "operation_id": operation_id,
                        "booking": asdict(existing.result), "replayed": True}
            operation = Operation(operation_id, self.user_id, action, original_resource.rid, 1,
                                  "pending", None, None, request=payload)
            self.repository.insert_operation(connection, operation, now)
            connection.execute("SAVEPOINT reservation_edit")
            try:
                booking = None
                if action != "add":
                    rows = connection.execute("""SELECT b.booking_id, b.uid FROM slot_users b JOIN slots s USING(sid)
                        WHERE b.status='active' AND s.rid=? AND s.starts_at=? AND s.ends_at=?""",
                        (original_resource.rid, start, end)).fetchall()
                    owned = [row for row in rows if row["uid"] == self.user_id]
                    if not owned:
                        if rows:
                            raise BookingError("forbidden", "This reservation belongs to another user.")
                        raise BookingError("reservation_not_found", "No active booking of yours matches this resource and interval.")
                    booking = self.repository.find_booking(connection, owned[0]["booking_id"])
                if action != "delete":
                    if target_start <= now:
                        raise BookingError("slot_expired", "New bookings must start in the future.")
                    if action == "update" and start <= now:
                        raise BookingError("slot_expired", "A booking that has started cannot be moved.")
                    self._check_capacity(connection, target_resource, target_start, target_end,
                                         excluding=booking.booking_id if booking else "")
                    slot = self._slot(connection, target_resource, target_start, target_end)
                if action == "add":
                    booking = Booking(str(uuid4()), self.user_id, slot.slot_id, "active", now)
                    self.repository.insert_booking(connection, booking, operation_id)
                elif action == "delete":
                    self.repository.cancel_booking(connection, booking.booking_id, now)
                    booking = self.repository.find_booking(connection, booking.booking_id)
                else:
                    connection.execute("UPDATE slot_users SET sid=?, updated_at=? WHERE booking_id=? AND uid=?",
                                       (slot.slot_id, now, booking.booking_id, self.user_id))
                    booking = self.repository.find_booking(connection, booking.booking_id)
                connection.execute("RELEASE reservation_edit")
            except BookingError as error:
                connection.execute("ROLLBACK TO reservation_edit")
                connection.execute("RELEASE reservation_edit")
                failure = error
                self.repository.update_operation(connection, operation_id, "failed", now, error_code=error.code)
            else:
                self.repository.update_operation(connection, operation_id, "succeeded", now, booking)
                return {"ok": True, "action": action, "operation_id": operation_id,
                        "booking": asdict(booking), "replayed": False}
        # Persist the failed operation without committing any partial reservation changes.
        raise failure
