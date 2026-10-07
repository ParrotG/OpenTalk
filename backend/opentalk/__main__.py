"""A local CLI for exercising the booking backend without model credentials."""

import argparse
import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from opentalk.config import load_config
from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError
from opentalk.storage.repository import BookingRepository
from opentalk.storage.admin import check, initialize_demo, inspect


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the OpenTalk booking backend.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, help="Override the configured database path.")
    parser.add_argument("--session", help="Deprecated compatibility option; business operations are user-scoped.")
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="Create configured slots for an explicit date.")
    seed.add_argument("--date", type=date.fromisoformat, required=True)
    available = commands.add_parser("available")
    available.add_argument("--date", type=date.fromisoformat, required=True)
    available.add_argument("--room")
    prepare = commands.add_parser("prepare")
    prepare.add_argument("slot_id")
    prepare.add_argument("--operation-id", required=True)
    prepare.add_argument("--supersedes")
    confirm = commands.add_parser("confirm")
    confirm.add_argument("operation_id")
    confirm.add_argument("--version", type=int, required=True)
    for command in ("booking", "operation", "invalidate"):
        commands.add_parser(command).add_argument("id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("booking_id")
    cancel.add_argument("--operation-id", required=True)
    commands.add_parser("operations", help="List the current user's business operations.")
    admin = commands.add_parser("admin", help="Initialize and inspect local business data as an administrator.")
    administrative = admin.add_subparsers(dest="admin_command", required=True)
    initialize = administrative.add_parser("init", help="Idempotently seed users, resources, intervals and occupancy.")
    initialize.add_argument("--start-date", type=date.fromisoformat)
    initialize.add_argument("--days", type=int, default=7)
    initialize.add_argument("--fixture", type=Path)
    inspection = administrative.add_parser("inspect", help="Inspect the schema or a business table/view.")
    inspection.add_argument("--table")
    inspection.add_argument("--limit", type=int, default=100)
    administrative.add_parser("check", help="Check integrity, references, capacity and duplicate bookings.")
    administrative.add_parser("migrate", help="Migrate an old business database, keeping a pre-migration backup.")
    administrative.add_parser("query", help="Run one read-only administrator SQL query.").add_argument("sql")
    commands.add_parser("smoke", help="Run a booking lifecycle against the selected database.")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        repository = BookingRepository(args.database or config.database_path)
        service = BookingService(
            repository, user_id=config.demo_user_id, timezone=config.timezone,
        )
        if args.command == "admin":
            if args.admin_command == "init":
                result = initialize_demo(repository, config, start_date=args.start_date,
                                         days=args.days, fixture_path=args.fixture)
            elif args.admin_command == "inspect":
                result = inspect(repository, args.table, args.limit)
            elif args.admin_command == "check":
                result = check(repository)
            elif args.admin_command == "query":
                from opentalk.tools.database_tools import DatabaseTools
                result = asyncio.run(DatabaseTools(service).query(args.sql))
            else:
                result = {"status": "migrated", **inspect(repository)}
        elif args.command == "seed":
            result = [service.seed_slot(
                room,
                datetime.combine(args.date, time(hour), service.timezone),
                datetime.combine(args.date, time(hour), service.timezone)
                + timedelta(minutes=config.slot_duration_minutes),
            ) for room in config.rooms for hour in config.slot_start_hours]
        elif args.command == "available":
            result = service.list_available_slots(args.date, args.room)
        elif args.command == "prepare":
            result = service.prepare_booking(args.slot_id, args.operation_id, args.supersedes)
        elif args.command == "confirm":
            result = service.confirm_booking(args.operation_id, args.version)
        elif args.command == "booking":
            result = service.get_booking(args.id)
        elif args.command == "operation":
            result = service.get_operation(args.id)
        elif args.command == "invalidate":
            result = service.invalidate_operation(args.id)
        elif args.command == "cancel":
            result = service.cancel_booking(args.booking_id, args.operation_id)
        elif args.command == "operations":
            result = service.list_operations()
        else:
            # Use a dedicated room so repeated runs do not clash with normal demo slots.
            day = datetime.now(service.timezone).date() + timedelta(days=1)
            start = datetime.combine(day, time(9), service.timezone)
            slot = service.seed_slot("Smoke Room", start, start + timedelta(hours=1))
            operation_id = f"smoke-{uuid4()}"
            proposal = service.prepare_booking(slot.slot_id, operation_id)
            booking = service.confirm_booking(operation_id, proposal.version)
            if service.confirm_booking(operation_id, proposal.version) != booking:
                raise RuntimeError("Confirmation retry did not return the original result.")
            cancelled = service.cancel_booking(booking.booking_id, f"{operation_id}-cancel")
            if service.get_booking(booking.booking_id) != cancelled:
                raise RuntimeError("Cancellation was not persisted.")
            result = {"status": "passed", "booking": booking, "cancellation": cancelled}
        print(json.dumps(result, default=encode, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (BookingError, ValueError, OSError) as error:
        print(json.dumps({"error": getattr(error, "code", "invalid_configuration"),
                          "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
