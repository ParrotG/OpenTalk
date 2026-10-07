"""Load session persistence, retention, and control API settings."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class SessionConfig:
    database_path: Path
    telemetry_database_path: Path
    heartbeat_seconds: float
    lease_seconds: float
    context_max_items: int
    pending_event_limit: int
    telemetry_max_events: int
    telemetry_retention_days: int
    api_host: str
    api_port: int
    public_livekit_url: str
    token_ttl_seconds: int
    max_session_seconds: int
    tracing_enabled: bool


def load_session_config(path=None):
    path = path or PROJECT_ROOT / "config/sessions.toml"
    try:
        with Path(path).open("rb") as file:
            values = tomllib.load(file)["sessions"]
        for key in ("database_path", "telemetry_database_path", "api_host"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"{key} must be a non-empty string.")
        for key in ("heartbeat_seconds", "lease_seconds"):
            value = values[key]
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be a finite positive number.")
        if values["lease_seconds"] < 3 * values["heartbeat_seconds"]:
            raise ValueError("The lease must span at least three heartbeats.")
        for key in ("context_max_items", "pending_event_limit", "telemetry_max_events",
                    "telemetry_retention_days", "api_port", "token_ttl_seconds", "max_session_seconds"):
            if type(values[key]) is not int or values[key] <= 0:
                raise ValueError(f"{key} must be a positive integer.")
        if values["api_port"] > 65535:
            raise ValueError("api_port is out of range.")
        if type(values["tracing_enabled"]) is not bool:
            raise ValueError("tracing_enabled must be a boolean.")
        url = urlparse(values["public_livekit_url"])
        if url.scheme not in ("ws", "wss") or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise ValueError("public_livekit_url must be a WebSocket URL without credentials.")
        for key in ("database_path", "telemetry_database_path"):
            target = Path(values[key])
            values[key] = target if target.is_absolute() else PROJECT_ROOT / target
        if values["database_path"].resolve() == values["telemetry_database_path"].resolve():
            raise ValueError("Session and telemetry databases must be separate.")
        return SessionConfig(**values)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid session configuration at {path}: {error}") from error
