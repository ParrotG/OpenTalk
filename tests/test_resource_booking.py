"""Exercise ownership, interval capacity, retries and migrations using real SQLite."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from opentalk.config import load_config
from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError
from opentalk.storage.admin import check, initialize_demo, inspect
from opentalk.storage.repository import BookingRepository
from opentalk.tools.database_tools import DatabaseTools

NOW = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def service(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "resources.sqlite3"), clock=lambda: NOW)
    service.seed_resource("Shared", capacity=2, resource_type="desk", location="Floor 2", metadata={"features": ["monitor"]})
    service.seed_resource("Private", capacity=1)
    return service


def owner(service, uid):
    return BookingService(service.repository, user_id=uid, clock=lambda: NOW)


def book(service, resource="Shared", start="2030-01-02T09:00", end="2030-01-02T10:00", key="one", **kwargs):
    return service.edit("add", resource, start, end, request_id=key, **kwargs)


def error(code, function):
    with pytest.raises(BookingError) as caught:
        function()
    assert caught.value.code == code


def test_other_users_visible_but_cannot_be_created_deleted_or_moved(service):
    bob = owner(service, "bob")
    reserved = book(bob)
    tools = DatabaseTools(service)
    rows = asyncio.run(tools.query("SELECT uid, resource_name FROM reservations WHERE status='active'"))["rows"]
    assert rows == [{"uid": "bob", "resource_name": "Shared"}]
    for action in ("delete", "update"):
        result = asyncio.run(tools.edit(action, "Shared", "2030-01-02T09:00", "2030-01-02T10:00", request_id=action))
        assert result["error"] == "forbidden"
    for action in ("add", "delete", "update"):
        result = asyncio.run(tools.edit(action, "Shared", "2030-01-02T09:00", "2030-01-02T10:00", uid="bob", request_id=action + "-uid"))
        assert result["error"] == "forbidden"
    assert bob.get_booking(reserved["booking"]["booking_id"]).status == "active"
    assert check(service.repository)["ok"]


def test_same_interval_uses_one_slot_and_only_current_user_changes(service):
    bob = owner(service, "bob")
    theirs = book(bob)
    ours = book(service)
    assert theirs["booking"]["slot_id"] == ours["booking"]["slot_id"]
    assert inspect(service.repository)["counts"]["slots"] == 1
    service.edit("update", "Shared", "2030-01-02T09:00", "2030-01-02T10:00",
                 new_resource="Private", request_id="move")
    assert bob.get_booking(theirs["booking"]["booking_id"]) == bob.get_operation(theirs["operation_id"]).result
    service.edit("delete", "Private", "2030-01-02T09:00", "2030-01-02T10:00", request_id="cancel")
    assert service.get_booking(ours["booking"]["booking_id"]).status == "cancelled"
    assert bob.get_booking(theirs["booking"]["booking_id"]).status == "active"


def test_capacity_uses_peak_concurrency_and_half_open_boundaries(service):
    book(owner(service, "bob"), end="2030-01-02T10:00")
    book(owner(service, "chen"), start="2030-01-02T10:00", end="2030-01-02T11:00")
    candidate = service.seed_slot("Shared", datetime(2030, 1, 2, 1, tzinfo=UTC), datetime(2030, 1, 2, 3, tzinfo=UTC))
    tools = DatabaseTools(service)
    row = asyncio.run(tools.query(f"SELECT * FROM slot_availability WHERE sid='{candidate.slot_id}'"))["rows"][0]
    assert row["peak_occupancy"] == 1 and row["remaining_capacity"] == 1
    book(service, end="2030-01-02T11:00")
    error("slot_unavailable", lambda: book(owner(service, "dina"), start="2030-01-02T09:30", end="2030-01-02T10:30"))
    # Back-to-back bookings fit even after earlier capacity was full.
    assert book(owner(service, "dina"), start="2030-01-02T11:00", end="2030-01-02T12:00", key="adjacent")["ok"]
    assert check(service.repository)["ok"]


def test_capacity_race_and_failed_retry_remain_durable(service):
    people = [owner(service, uid) for uid in ("bob", "chen", "dina")]
    def compete(person):
        try:
            return book(person)["ok"]
        except BookingError as caught:
            return caught.code
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(compete, people))
    assert results.count(True) == 2 and results.count("slot_unavailable") == 1
    winner = next(person for person, result in zip(people, results) if result is True)
    loser = next(person for person, result in zip(people, results) if result != True)
    winner.edit("delete", "Shared", "2030-01-02T09:00", "2030-01-02T10:00", request_id="cancel")
    error("slot_unavailable", lambda: book(loser))
    assert book(loser, key="new-request")["ok"]
    assert check(service.repository)["ok"]


def test_update_failure_keeps_original_and_records_one_failed_operation(service):
    original = book(service)
    book(owner(service, "bob"), resource="Private")
    error("slot_unavailable", lambda: service.edit("update", "Shared", "2030-01-02T09:00", "2030-01-02T10:00",
                                                   new_resource="Private", request_id="move"))
    assert service.get_booking(original["booking"]["booking_id"]).slot_id == original["booking"]["slot_id"]
    error("slot_unavailable", lambda: service.edit("update", "Shared", "2030-01-02T09:00", "2030-01-02T10:00",
                                                   new_resource="Private", request_id="move"))
    assert [item.status for item in service.list_operations()] == ["succeeded", "failed"]
    assert check(service.repository)["ok"]


def test_request_key_is_owner_scoped_and_changed_arguments_cannot_write_twice(service):
    first = book(service)
    assert book(service)["replayed"]
    error("idempotency_conflict", lambda: book(service, start="2030-01-02T10:00", end="2030-01-02T11:00"))
    second = book(owner(service, "bob"))
    assert first["operation_id"] != second["operation_id"]
    assert len(service.list_operations()) == 1
    error("slot_unavailable", lambda: book(service, key="other-request"))
    assert inspect(service.repository)["counts"]["slot_users"] == 2


def test_unknown_resources_past_times_and_metadata_constraints(service):
    error("resource_not_found", lambda: book(service, resource="Imaginary"))
    error("slot_expired", lambda: book(service, start="2029-12-31T09:00", end="2029-12-31T10:00"))
    error("invalid_time", lambda: book(service, end="2030-01-02T09:00"))
    error("invalid_resource", lambda: service.seed_resource("Invalid", metadata=[]))
    with service.repository.transaction() as connection:
        for bad in (0, -1, 1.5):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute("UPDATE resources SET capacity=? WHERE name='Shared'", (bad,))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE resources SET metadata='[]' WHERE name='Shared'")
    assert check(service.repository)["ok"]


def test_demo_initialization_is_repeatable_and_cancellation_is_not_resurrected(tmp_path):
    repository = BookingRepository(tmp_path / "demo.sqlite3")
    config = replace(load_config(), database_path=repository.database_path)
    first = initialize_demo(repository, config, start_date=date(2030, 1, 2), days=2)
    assert first["counts"] == {"users": 4, "resources": 4, "slots": 32, "slot_users": 14, "operations": 14}
    second = initialize_demo(repository, config, start_date=date(2030, 1, 2), days=2)
    assert second["new_occupancies"] == 0 and not second["skipped"] and second["counts"] == first["counts"]
    service = BookingService(repository, clock=lambda: NOW)
    service.edit("delete", "Room A", "2030-01-02T14:00", "2030-01-02T15:00", request_id="cancel")
    initialize_demo(repository, config, start_date=date(2030, 1, 2), days=2)
    assert [item.status for item in service.list_operations()] == ["succeeded", "succeeded", "succeeded"]
    with repository.connection() as connection:
        assert connection.execute("SELECT count(*) FROM slot_users WHERE uid='demo-user' AND status='cancelled'").fetchone()[0] == 1
    assert check(repository)["ok"]


def legacy_database(path, *, invalid=False):
    with sqlite3.connect(path) as connection:
        connection.executescript(Path(__file__).with_name("fixtures").joinpath("booking_v1.sql").read_text())
        start, end = "2030-01-02T01:00:00.000000+00:00", "2030-01-02T02:00:00.000000+00:00"
        connection.execute("INSERT INTO slots VALUES ('slot-old', 'Room A', ?, ?)", (start, end))
        snapshot = {"booking_id": "booking-old", "user_id": "demo-user", "slot_id": "slot-old", "status": "active", "created_at": NOW.isoformat()}
        connection.execute("""INSERT INTO operations VALUES ('operation-old', 'demo-user', 'agent-session-old',
            'book', 'slot-old', 1, NULL, 'succeeded', ?, NULL, ?, ?)""",
            (json.dumps(snapshot), NOW.isoformat(), NOW.isoformat()))
        connection.execute("INSERT INTO bookings VALUES ('booking-old', 'demo-user', 'slot-old', 'operation-old', 'active', ?)", (NOW.isoformat(),))
        connection.execute("INSERT INTO events VALUES ('agent-log', 'operation-old', 'agent-session-old', 'prepared', '{}', ?)", (NOW.isoformat(),))
    if invalid:
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE bookings SET slot_id='missing'")
    return snapshot


def test_legacy_migration_preserves_bookings_and_operations_but_removes_agent_records(tmp_path):
    path = tmp_path / "old.sqlite3"
    snapshot = legacy_database(path)
    repository = BookingRepository(path)
    service = BookingService(repository, clock=lambda: NOW)
    assert asdict(service.get_booking("booking-old")) == snapshot
    assert asdict(service.get_operation("operation-old").result) == snapshot
    assert service.confirm_booking("operation-old", 1) == service.get_booking("booking-old")
    info = inspect(repository)
    assert {table["name"] for table in info["tables"]} == {"users", "resources", "slots", "slot_users", "operations"}
    assert all("session_id" not in table["sql"] for table in info["tables"])
    backup = path.with_name(path.name + ".pre-resources-v1.bak")
    with sqlite3.connect(backup) as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    BookingRepository(path)
    assert inspect(repository)["counts"] == info["counts"]
    assert check(repository)["ok"]


def test_invalid_legacy_migration_rolls_back_schema_and_data(tmp_path):
    path = tmp_path / "invalid.sqlite3"
    legacy_database(path, invalid=True)
    with pytest.raises(ValueError, match="foreign keys"):
        BookingRepository(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT slot_id FROM bookings").fetchone()[0] == "missing"
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
