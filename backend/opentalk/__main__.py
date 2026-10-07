"""A local CLI for exercising the booking backend without model credentials."""

import argparse
import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import date
from pathlib import Path

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
    parser = argparse.ArgumentParser(description="Administer the OpenTalk resource-booking database.")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--database", type=Path, help="Override the configured database path.")
    commands = parser.add_subparsers(dest="command", required=True)
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
    administrative.add_parser("query", help="Run one read-only administrator SQL query.").add_argument("sql")
    args = parser.parse_args()

    try:
        config = load_config(args.config)
        repository = BookingRepository(args.database or config.database_path)
        service = BookingService(
            repository, user_id=config.demo_user_id, timezone=config.timezone,
        )
        if args.admin_command == "init":
            result = initialize_demo(repository, config, start_date=args.start_date,
                                     days=args.days, fixture_path=args.fixture)
        elif args.admin_command == "inspect":
            result = inspect(repository, args.table, args.limit)
        elif args.admin_command == "check":
            result = check(repository)
        else:
            from opentalk.tools.database_tools import DatabaseTools
            result = asyncio.run(DatabaseTools(service).query(args.sql))
        print(json.dumps(result, default=encode, ensure_ascii=False, indent=2))
        return 1 if isinstance(result, dict) and result.get("ok") is False else 0
    except (BookingError, ValueError, OSError) as error:
        print(json.dumps({"error": getattr(error, "code", "invalid_configuration"),
                          "message": str(error)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
