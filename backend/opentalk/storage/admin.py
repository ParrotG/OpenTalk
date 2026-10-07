"""Local administrator initialization and inspection, independent of agent sessions."""

import json
from datetime import date, datetime, time, timedelta
from pathlib import Path

from opentalk.config import PROJECT_ROOT
from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError, Resource, User
from opentalk.storage.repository import stable_id

TABLES = ("users", "resources", "slots", "slot_users", "operations")


def initialize_demo(repository, configuration, *, start_date=None, days=7, fixture_path=None):
    if type(days) is not int or not 1 <= days <= 366:
        raise BookingError("invalid_input", "Days must be between 1 and 366.")
    profile = json.loads(Path(fixture_path or PROJECT_ROOT / "config/booking_demo.json").read_text(encoding="utf-8"))
    users = [User(**item) for item in profile["users"]]
    resources = [Resource(rid=stable_id("resource", item["name"]), **item) for item in profile["resources"]]
    if configuration.demo_user_id not in {user.uid for user in users}:
        users.append(User(configuration.demo_user_id, configuration.demo_user_id, "Demo"))
    for resource in resources:
        if (not resource.name.strip() or type(resource.capacity) is not int or resource.capacity < 1
                or not isinstance(resource.metadata, dict)):
            raise BookingError("invalid_resource", "Demo resources require a name, positive capacity and object metadata.")
    with repository.transaction() as connection:
        for user in users:
            repository.insert_user(connection, user)
            # Enrich placeholder records from old databases without overwriting edited descriptions.
            connection.execute("UPDATE users SET name=?, department=? WHERE uid=? AND name=uid AND department='Unspecified'",
                               (user.name, user.department, user.uid))
        for resource in resources:
            repository.insert_resource(connection, resource)
            connection.execute("""UPDATE resources SET type=?, location=?, capacity=?, metadata=?
                WHERE rid=? AND location='Unspecified' AND metadata='{}' AND capacity<=?""",
                (resource.type, resource.location, resource.capacity, json.dumps(resource.metadata, sort_keys=True),
                 resource.rid, resource.capacity))
    service = BookingService(repository, user_id=configuration.demo_user_id, timezone=configuration.timezone)
    start_date = start_date or service.clock().astimezone(service.timezone).date() + timedelta(days=1)
    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise BookingError("invalid_input", "A calendar start date is required.")
    occupied, skipped = 0, []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        for resource in resources:
            for hour in configuration.slot_start_hours:
                start = datetime.combine(day, time(hour), service.timezone)
                service.seed_slot(resource.name, start, start + timedelta(minutes=configuration.slot_duration_minutes))
        for entry in profile["occupancy"]:
            start = datetime.combine(day, time(entry["hour"]), service.timezone)
            end = start + timedelta(minutes=configuration.slot_duration_minutes)
            for uid in entry["uids"]:
                if uid not in {user.uid for user in users}:
                    raise BookingError("invalid_input", "Demo occupancy refers to an unknown user.")
                owner = BookingService(repository, user_id=uid, timezone=configuration.timezone)
                # Historical fixtures may be useful for inspection; seed against a fixed earlier clock.
                owner.clock = lambda start=start: start - timedelta(seconds=1)
                request_id = f"seed:{entry['resource']}:{start.isoformat()}:{end.isoformat()}:{uid}"
                try:
                    result = owner.edit("add", entry["resource"], start, end, request_id=request_id)
                    occupied += int(not result["replayed"])
                except BookingError as error:
                    skipped.append({"uid": uid, "resource": entry["resource"], "date": day.isoformat(), "error": error.code})
    return {"status": "initialized", "start_date": start_date.isoformat(), "days": days,
            "current_user": configuration.demo_user_id, "new_occupancies": occupied,
            "skipped": skipped, "database": str(repository.database_path), "counts": inspect(repository)["counts"]}


def inspect(repository, table=None, limit=100):
    if table is not None and table not in TABLES + ("reservations", "slot_availability"):
        raise BookingError("invalid_input", "Select a business table or reservation view.")
    if type(limit) is not int or not 1 <= limit <= 10000:
        raise BookingError("invalid_input", "Limit must be between 1 and 10000.")
    with repository.connection() as connection:
        if table:
            rows = connection.execute(f"SELECT * FROM {table} LIMIT ?", (limit + 1,)).fetchall()
            return {"table": table, "rows": [dict(row) for row in rows[:limit]], "truncated": len(rows) > limit}
        return {"schema_version": connection.execute("PRAGMA user_version").fetchone()[0],
                "counts": {name: connection.execute(f"SELECT count(*) FROM {name}").fetchone()[0] for name in TABLES},
                "tables": [dict(row) for row in connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name")]}


def check(repository):
    with repository.connection() as connection:
        integrity = [row[0] for row in connection.execute("PRAGMA integrity_check")]
        foreign_keys = [dict(row) for row in connection.execute("PRAGMA foreign_key_check")]
        capacity = [dict(row) for row in connection.execute(
            "SELECT * FROM slot_availability WHERE remaining_capacity < 0")]
        duplicates = [dict(row) for row in connection.execute("""SELECT a.booking_id, b.booking_id AS other_booking_id
            FROM reservations a JOIN reservations b ON a.uid=b.uid AND a.rid=b.rid AND a.booking_id<b.booking_id
            WHERE a.status='active' AND b.status='active' AND a.starts_at<b.ends_at AND a.ends_at>b.starts_at""")]
    return {"ok": integrity == ["ok"] and not foreign_keys and not capacity and not duplicates,
            "integrity": integrity, "foreign_keys": foreign_keys,
            "capacity_violations": capacity, "overlapping_user_bookings": duplicates}
