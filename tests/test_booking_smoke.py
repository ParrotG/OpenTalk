"""Smoke coverage for real SQLite state transitions and retries."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest

from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError
from opentalk.storage.repository import BookingRepository

NOW = datetime(2030, 1, 1, tzinfo=UTC)
DAY = date(2030, 1, 2)


@pytest.fixture
def service(tmp_path):
    return BookingService(BookingRepository(tmp_path / "booking.sqlite3"), clock=lambda: NOW)


def seed(service, hour=1, room="Room A"):
    start = datetime(2030, 1, 2, hour, tzinfo=UTC)
    return service.seed_slot(room, start, start + timedelta(hours=1))


def assert_error(code, call):
    with pytest.raises(BookingError) as error:
        call()
    assert error.value.code == code


def test_lifecycle_seed_retry_restart_and_cancel(service):
    slot = seed(service)
    assert seed(service) == slot
    assert service.list_available_slots(DAY) == [slot]
    proposal = service.prepare_booking(slot.slot_id, "book-1")
    assert proposal.status == "pending"
    assert service.list_available_slots(DAY) == [slot]
    assert service.prepare_booking(slot.slot_id, "book-1") == proposal
    booking = service.confirm_booking("book-1", proposal.version)
    assert service.list_available_slots(DAY) == []
    restarted = BookingService(service.repository, clock=lambda: NOW)
    # A lost response can be recovered after process restart without another write.
    assert restarted.get_operation("book-1").result == booking
    assert restarted.confirm_booking("book-1", 1) == booking
    assert len(service.list_operations()) == 1
    cancelled = restarted.cancel_booking(booking.booking_id, "cancel-1")
    assert cancelled.status == "cancelled"
    assert restarted.cancel_booking(booking.booking_id, "cancel-1") == cancelled
    assert restarted.get_booking(booking.booking_id) == cancelled
    assert restarted.list_available_slots(DAY) == [slot]
    assert len(restarted.list_operations()) == 2
    proposal2 = restarted.prepare_booking(slot.slot_id, "book-2")
    assert restarted.confirm_booking("book-2", proposal2.version).booking_id != booking.booking_id


def test_replacement_invalidates_old_confirmation(service):
    first, second = seed(service), seed(service, 2)
    service.prepare_booking(first.slot_id, "old")
    replacement = service.prepare_booking(second.slot_id, "new", supersedes="old")
    assert replacement.version == 2
    assert service.get_operation("old").status == "invalidated"
    assert_error("operation_not_pending", lambda: service.confirm_booking("old", 1))
    assert_error("confirmation_mismatch", lambda: service.confirm_booking("new", 1))
    assert service.confirm_booking("new", 2).slot_id == second.slot_id
    assert service.list_available_slots(DAY) == [first]
    assert_error("idempotency_conflict", lambda: service.prepare_booking(second.slot_id, "new"))


def test_competing_confirmations_have_one_winner(service):
    slot = seed(service)
    service.prepare_booking(slot.slot_id, "first")
    service.prepare_booking(slot.slot_id, "second")

    def confirm(operation_id):
        try:
            return service.confirm_booking(operation_id, 1)
        except BookingError as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(confirm, ["first", "second"]))
    assert sum(result == "slot_unavailable" for result in results) == 1
    operations = [service.get_operation(key) for key in ("first", "second")]
    assert sorted(op.status for op in operations) == ["failed", "succeeded"]
    failed = next(op for op in operations if op.status == "failed")
    assert_error("slot_unavailable", lambda: service.confirm_booking(failed.operation_id, 1))
    assert len(service.list_operations()) == 2


def test_request_keys_and_ownership(service):
    first, second = seed(service), seed(service, 2)
    service.prepare_booking(first.slot_id, "same")
    assert_error("idempotency_conflict", lambda: service.prepare_booking(second.slot_id, "same"))
    other = BookingService(service.repository, user_id="another-user", clock=lambda: NOW)
    assert_error("operation_not_found", lambda: other.get_operation("same"))
    assert other.list_operations() == []
    another_session = BookingService(service.repository, session_id="another-session", clock=lambda: NOW)
    assert another_session.get_operation("same") == service.get_operation("same")
    booking = service.confirm_booking("same", 1)
    assert_error("booking_not_found", lambda: other.cancel_booking(booking.booking_id, "cancel"))
    assert_error("idempotency_conflict", lambda: service.cancel_booking(booking.booking_id, "same"))
    assert service.get_booking(booking.booking_id).status == "active"


def test_discard_and_expiry(service):
    slot = seed(service)
    service.prepare_booking(slot.slot_id, "discard")
    discarded = service.invalidate_operation("discard")
    assert service.invalidate_operation("discard") == discarded
    assert_error("operation_not_pending", lambda: service.confirm_booking("discard", 1))
    service.prepare_booking(slot.slot_id, "expire")
    later = BookingService(service.repository, clock=lambda: datetime(2030, 1, 3, tzinfo=UTC))
    assert_error("slot_expired", lambda: later.confirm_booking("expire", 1))
    assert later.get_operation("expire").status == "failed"
    assert later.list_available_slots(DAY) == []


def test_slot_validation_and_local_date(service):
    # January 1 UTC can already be January 2 in Asia/Shanghai.
    start = datetime(2030, 1, 1, 17, tzinfo=UTC)
    slot = service.seed_slot("Room A", start, start + timedelta(hours=1))
    assert service.list_available_slots(DAY, "Room A") == [slot]
    assert service.list_available_slots(date(2030, 1, 1)) == []
    overlapping = service.seed_slot("Room A", start + timedelta(minutes=30), start + timedelta(hours=2))
    assert service.list_available_slots(DAY, "Room A") == [slot, overlapping]
    assert_error("invalid_time", lambda: service.seed_slot(
        "Room A", datetime(2030, 1, 2), datetime(2030, 1, 2, 1)))
