"""Load public backend settings relative to the project root."""

import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class BackendConfig:
    database_path: Path
    timezone: str
    demo_user_id: str
    slot_start_hours: tuple[int, ...]
    slot_duration_minutes: int


def load_config(path: Path | None = None) -> BackendConfig:
    path = path or PROJECT_ROOT / "config/backend.toml"
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)["booking"]
        database_path = data["database_path"]
        timezone = data["timezone"]
        user_id = data["demo_user_id"]
        hours = data["slot_start_hours"]
        duration = data["slot_duration_minutes"]
        if not all(isinstance(value, str) and value.strip()
                   for value in (database_path, timezone, user_id)):
            raise ValueError("Database path, timezone, and user ID must be non-empty strings.")
        ZoneInfo(timezone)
        if not isinstance(hours, list) or not hours or not all(
            type(hour) is int and 0 <= hour <= 23 for hour in hours
        ) or len(set(hours)) != len(hours):
            raise ValueError("Slot start hours must be unique integers from 0 to 23.")
        if type(duration) is not int or not 1 <= duration <= 1440:
            raise ValueError("Slot duration must be between 1 and 1440 minutes.")
        ordered = sorted(hours)
        if any((right - left) * 60 < duration for left, right in zip(ordered, ordered[1:])):
            raise ValueError("Configured slot times must not overlap.")
        if ordered[-1] * 60 + duration > 1440:
            raise ValueError("Configured slots must end within the selected day.")
    except (OSError, KeyError, TypeError, ValueError, ZoneInfoNotFoundError) as error:
        raise ValueError(f"Invalid backend configuration at {path}: {error}") from error
    target = Path(database_path)
    return BackendConfig(
        target if target.is_absolute() else PROJECT_ROOT / target,
        timezone, user_id, tuple(hours), duration,
    )
