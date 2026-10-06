"""Deterministic booking rules with durable idempotency and confirmation binding."""

from datetime import UTC, date, datetime
from typing import Callable
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from opentalk.domain.models import Booking, BookingError, Operation, Slot
from opentalk.storage.repository import BookingRepository


class BookingService:
    def __init__(self, repository: BookingRepository, user_id: str = "demo-user",
                 session_id: str = "demo-session", timezone: str = "Asia/Shanghai",
                 clock: Callable[[], datetime] | None = None):
        self.repository = repository
        self.user_id = self._identifier(user_id)
        self.session_id = self._identifier(session_id)
        self.timezone = ZoneInfo(timezone)
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _identifier(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise BookingError("invalid_input", "Identifiers must be non-empty strings.")
        return value

    @staticmethod
    def _utc(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise BookingError("invalid_time", "A timezone-aware timestamp is required.")
        return value.astimezone(UTC).isoformat(timespec="microseconds")

    def _owned_operation(self, connection, operation_id: str) -> Operation:
        operation = self.repository.find_operation(connection, operation_id)
        if operation is None or operation.user_id != self.user_id or operation.session_id != self.session_id:
            raise BookingError("operation_not_found", "The operation was not found in this session.")
        return operation

    def _owned_booking(self, connection, booking_id: str) -> Booking:
        booking = self.repository.find_booking(connection, booking_id)
        if booking is None or booking.user_id != self.user_id:
            raise BookingError("booking_not_found", "The booking was not found for this user.")
        return booking

    def seed_slot(self, room: str, starts_at: datetime, ends_at: datetime) -> Slot:
        self._identifier(room)
        start, end = self._utc(starts_at), self._utc(ends_at)
        if start >= end:
            raise BookingError("invalid_time", "The slot must end after it starts.")
        slot = Slot(str(uuid5(NAMESPACE_URL, f"opentalk:slot:{room}:{start}:{end}")),
                    room, start, end)
        with self.repository.transaction() as connection:
            existing = self.repository.find_slot(connection, slot.slot_id)
            if existing:
                return existing
            if self.repository.overlapping_slot(connection, slot):
                raise BookingError("overlapping_slot", "Room slots must not overlap.")
            self.repository.insert_slot(connection, slot)
        return slot

    def list_available_slots(self, day: date, room: str | None = None) -> list[Slot]:
        if not isinstance(day, date) or isinstance(day, datetime):
            raise BookingError("invalid_input", "A calendar date is required.")
        now = self._utc(self.clock())
        with self.repository.connection() as connection:
            slots = self.repository.available_slots(connection)
        return [slot for slot in slots
                if slot.starts_at > now
                and datetime.fromisoformat(slot.starts_at).astimezone(self.timezone).date() == day
                and (room is None or slot.room == room)]

    def prepare_booking(self, slot_id: str, operation_id: str,
                        supersedes: str | None = None) -> Operation:
        self._identifier(operation_id)
        self._identifier(slot_id)
        if supersedes == operation_id:
            raise BookingError("invalid_input", "A replacement requires a new operation ID.")
        now = self._utc(self.clock())
        with self.repository.transaction() as connection:
            existing = self.repository.find_operation(connection, operation_id)
            if existing:
                self._check_request(existing, "book", slot_id)
                if existing.supersedes != supersedes:
                    raise BookingError("idempotency_conflict", "The replacement request has changed.")
                return existing
            slot = self.repository.find_slot(connection, slot_id)
            if slot is None:
                raise BookingError("slot_not_found", "The slot was not found.")
            if slot.starts_at <= now:
                raise BookingError("slot_expired", "The slot has already started.")
            if self.repository.occupied(connection, slot_id):
                raise BookingError("slot_unavailable", "The slot is already booked.")
            version = 1
            if supersedes is not None:
                old = self._owned_operation(connection, supersedes)
                if old.kind != "book" or old.status != "pending":
                    raise BookingError("operation_not_pending", "Only a pending booking can be replaced.")
                version = old.version + 1
                self.repository.update_operation(connection, old.operation_id, "invalidated", now)
                self.repository.record_event(connection, old, "invalidated", now,
                                             {"replacement_operation_id": operation_id})
            operation = Operation(operation_id, self.user_id, self.session_id,
                                  "book", slot_id, version, "pending", None, None, supersedes)
            self.repository.insert_operation(connection, operation, now)
            self.repository.record_event(connection, operation, "prepared", now,
                                         {"slot_id": slot_id, "version": version})
            return operation

    def _check_request(self, operation: Operation, kind: str, target_id: str) -> None:
        if (operation.user_id, operation.session_id, operation.kind, operation.target_id) != (
            self.user_id, self.session_id, kind, target_id
        ):
            raise BookingError("idempotency_conflict", "The operation ID belongs to a different request.")

    def confirm_booking(self, operation_id: str, expected_version: int) -> Booking:
        now = self._utc(self.clock())
        error_code = None
        with self.repository.transaction() as connection:
            operation = self._owned_operation(connection, operation_id)
            if operation.kind != "book" or type(expected_version) is not int or operation.version != expected_version:
                raise BookingError("confirmation_mismatch", "Confirmation must match the booking version.")
            if operation.status == "succeeded":
                return operation.result
            if operation.status == "failed":
                raise BookingError(operation.error_code, "The booking operation previously failed.")
            if operation.status != "pending":
                raise BookingError("operation_not_pending", "The booking operation is no longer pending.")
            slot = self.repository.find_slot(connection, operation.target_id)
            if slot.starts_at <= now:
                error_code = "slot_expired"
            elif self.repository.occupied(connection, slot.slot_id):
                error_code = "slot_unavailable"
            if error_code:
                self.repository.update_operation(connection, operation_id, "failed", now,
                                                 error_code=error_code)
                self.repository.record_event(connection, operation, "failed", now,
                                             {"error_code": error_code})
            else:
                booking = Booking(str(uuid4()), self.user_id, slot.slot_id, "active", now)
                self.repository.insert_booking(connection, booking, operation_id)
                self.repository.update_operation(connection, operation_id, "succeeded", now, booking)
                self.repository.record_event(connection, operation, "succeeded", now,
                                             {"booking_id": booking.booking_id})
                return booking
        # Commit the failed outcome before reporting it to the caller.
        raise BookingError(error_code, "The slot is no longer available for booking.")

    def get_operation(self, operation_id: str) -> Operation:
        with self.repository.connection() as connection:
            return self._owned_operation(connection, operation_id)

    def invalidate_operation(self, operation_id: str) -> Operation:
        """Discard an unconfirmed proposal without changing committed bookings."""
        now = self._utc(self.clock())
        with self.repository.transaction() as connection:
            operation = self._owned_operation(connection, operation_id)
            if operation.status == "invalidated":
                return operation
            if operation.status != "pending":
                raise BookingError("operation_not_pending", "Only a pending operation can be discarded.")
            self.repository.update_operation(connection, operation_id, "invalidated", now)
            self.repository.record_event(connection, operation, "invalidated", now, {})
            return self._owned_operation(connection, operation_id)

    def get_booking(self, booking_id: str) -> Booking:
        with self.repository.connection() as connection:
            return self._owned_booking(connection, booking_id)

    def cancel_booking(self, booking_id: str, operation_id: str) -> Booking:
        """Execute an explicit cancellation request with its own idempotency key."""
        self._identifier(operation_id)
        now = self._utc(self.clock())
        with self.repository.transaction() as connection:
            existing = self.repository.find_operation(connection, operation_id)
            if existing:
                self._check_request(existing, "cancel", booking_id)
                return existing.result
            booking = self._owned_booking(connection, booking_id)
            operation = Operation(operation_id, self.user_id, self.session_id,
                                  "cancel", booking_id, 1, "pending", None, None)
            self.repository.insert_operation(connection, operation, now)
            self.repository.cancel_booking(connection, booking_id)
            result = Booking(booking.booking_id, booking.user_id, booking.slot_id,
                             "cancelled", booking.created_at)
            self.repository.update_operation(connection, operation_id, "succeeded", now, result)
            self.repository.record_event(connection, operation, "succeeded", now,
                                         {"booking_id": booking_id, "status": "cancelled"})
            return result

    def list_events(self) -> list[dict]:
        with self.repository.connection() as connection:
            return self.repository.events(connection, self.session_id, self.user_id)
