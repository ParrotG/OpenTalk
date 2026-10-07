"""Verify SQL read-only enforcement and direct transactional reservation edits."""

import asyncio
from datetime import UTC, datetime

import pytest

from opentalk.domain.booking_service import BookingService
from opentalk.storage.repository import BookingRepository
from opentalk.tools.database_tools import DatabaseTools


def make_tools(tmp_path):
    service = BookingService(BookingRepository(tmp_path / "database.sqlite3"),
                             clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    service.seed_slot("Room A", datetime(2030, 1, 2, 1, tzinfo=UTC), datetime(2030, 1, 2, 2, tzinfo=UTC))
    service.seed_slot("Room B", datetime(2030, 1, 3, 1, tzinfo=UTC), datetime(2030, 1, 3, 2, tzinfo=UTC))
    return DatabaseTools(service)


def test_sql_ranges_joins_ctes_schema_and_truncation(tmp_path):
    tools = make_tools(tmp_path)

    async def scenario():
        result = await tools.query("WITH rooms AS (SELECT DISTINCT room FROM slots) SELECT * FROM rooms ORDER BY room")
        assert [row["room"] for row in result["rows"]] == ["Room A", "Room B"]
        assert (await tools.query("SELECT s.room FROM slots s LEFT JOIN bookings b USING(slot_id) WHERE b.booking_id IS NULL"))["ok"]
        assert (await tools.query("SELECT name, sql FROM sqlite_master WHERE type='table'"))["ok"]
        tools.max_rows = 1
        result = await tools.query("SELECT * FROM slots ORDER BY starts_at")
        assert len(result["rows"]) == 1 and result["truncated"]
    asyncio.run(scenario())


@pytest.mark.parametrize("sql", [
    "DELETE FROM slots", "UPDATE slots SET room='X'", "DROP TABLE bookings",
    "INSERT INTO slots VALUES ('x','X','a','b')", "PRAGMA writable_schema=ON",
    "ATTACH DATABASE ':memory:' AS other", "BEGIN", "SELECT 1; DELETE FROM slots",
    "SELECT load_extension('anything')", "CREATE TEMP TABLE x(a)",
])
def test_sql_rejects_writes_and_control_statements(tmp_path, sql):
    tools = make_tools(tmp_path)
    assert asyncio.run(tools.query(sql))["ok"] is False
    assert len(asyncio.run(tools.query("SELECT * FROM slots"))["rows"]) == 2


def test_query_vm_budget_stops_unbounded_recursion(tmp_path):
    tools = make_tools(tmp_path)
    tools.max_vm_steps = 2000
    result = asyncio.run(tools.query(
        "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n) SELECT sum(x) FROM n"))
    assert result["error"] == "query_rejected"


def test_free_interval_add_update_delete_retries_and_audit(tmp_path):
    tools = make_tools(tmp_path)

    async def scenario():
        original = ("Room A", "2030-01-02T11:15:00", "2030-01-02T12:45:00")
        added = await tools.edit("add", *original, request_id="turn-1")
        assert added["ok"] and added["booking"]["status"] == "active"
        assert (await tools.edit("add", *original, request_id="turn-1"))["replayed"]
        assert (await tools.edit("add", *original, request_id="another-turn"))["error"] == "slot_unavailable"
        moved = await tools.edit("update", *original, new_room="Room B", new_starts_at="2030-01-03T14:00:00+08:00",
                                 new_ends_at="2030-01-03T15:00:00+08:00", request_id="turn-2")
        assert moved["ok"] and moved["booking"]["booking_id"] == added["booking"]["booking_id"]
        deleted = await tools.edit("delete", "Room B", "2030-01-03T14:00:00", "2030-01-03T15:00:00", request_id="turn-3")
        assert deleted["ok"] and deleted["booking"]["status"] == "cancelled"
        assert (await tools.edit("delete", "Room B", "2030-01-03T14:00:00", "2030-01-03T15:00:00", request_id="turn-3"))["replayed"]
        assert len(tools.service.list_events()) == 3
        assert len((await tools.query("SELECT * FROM bookings"))["rows"]) == 1
    asyncio.run(scenario())


def test_conflicting_update_rolls_back_original_booking(tmp_path):
    tools = make_tools(tmp_path)

    async def scenario():
        first = ("Room A", "2030-01-02T09:00:00", "2030-01-02T10:00:00")
        second = ("Room A", "2030-01-02T10:00:00", "2030-01-02T11:00:00")
        added = await tools.edit("add", *first, request_id="one")
        await tools.edit("add", *second, request_id="two")
        result = await tools.edit("update", *first, new_starts_at="2030-01-02T10:30:00",
                                  new_ends_at="2030-01-02T11:30:00", request_id="three")
        assert result["error"] == "slot_unavailable"
        assert tools.service.get_booking(added["booking"]["booking_id"]).slot_id == added["booking"]["slot_id"]
        assert len(tools.service.list_events()) == 2
    asyncio.run(scenario())


def test_invalid_intervals_and_missing_reservations(tmp_path):
    tools = make_tools(tmp_path)
    assert asyncio.run(tools.edit("add", "A", "invalid", "invalid", request_id="x"))["error"] == "invalid_time"
    assert asyncio.run(tools.edit("add", "A", "2030-01-02T11:00", "2030-01-02T10:00", request_id="x"))["error"] == "invalid_time"
    assert asyncio.run(tools.edit("delete", "A", "2030-01-02T09:00", "2030-01-02T10:00", request_id="x"))["error"] == "reservation_not_found"
