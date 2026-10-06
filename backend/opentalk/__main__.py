"""A local CLI for exercising the booking backend without model credentials."""

import argparse
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4

from opentalk.config import load_config
from opentalk.domain.booking_service import BookingService
from opentalk.domain.models import BookingError
from opentalk.storage.repository import BookingRepository


def encode(value):
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Cannot serialize {type(value).__name__}.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Exercise the OpenTalk booking backend.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, help="Override the configured database path.")
    parser.add_argument("--session", default="demo-session")
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
    commands.add_parser("events")
    commands.add_parser("smoke", help="Run a booking lifecycle against the selected database.")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        service = BookingService(
            BookingRepository(args.database or config.database_path),
            user_id=config.demo_user_id, session_id=args.session, timezone=config.timezone,
        )
        if args.command == "seed":
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
        elif args.command == "events":
            result = service.list_events()
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
        return 0
    except (BookingError, ValueError) as error:
        print(json.dumps({"error": getattr(error, "code", "invalid_configuration"),
                          "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
