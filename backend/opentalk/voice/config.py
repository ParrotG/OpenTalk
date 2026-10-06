"""Load session and Silero settings from public configuration."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class VoiceConfig:
    agent_name: str
    livekit_url: str
    log_directory: Path
    log_flush_interval_seconds: float
    max_tool_steps: int
    min_interruption_duration: float
    endpoint_min_delay: float
    endpoint_max_delay: float
    vad_options: dict


def load_voice_config(path: Path | None = None) -> VoiceConfig:
    path = path or PROJECT_ROOT / "config/voice.toml"
    try:
        with path.open("rb") as file:
            document = tomllib.load(file)
        values, vad = document["voice"], document["vad"]
        for key in ("agent_name", "livekit_url", "log_directory"):
            if not isinstance(values[key], str) or not values[key].strip():
                raise ValueError(f"{key} must be a non-empty string.")
        endpoint = urlparse(values["livekit_url"])
        if endpoint.scheme not in ("ws", "wss") or not endpoint.netloc:
            raise ValueError("livekit_url must be a WebSocket URL.")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("livekit_url must not contain credentials, query parameters, or fragments.")
        if type(values["max_tool_steps"]) is not int or values["max_tool_steps"] <= 0:
            raise ValueError("max_tool_steps must be a positive integer.")
        for table, keys in (
            (values, ("min_interruption_duration", "endpoint_min_delay", "endpoint_max_delay", "log_flush_interval_seconds")),
            (vad, ("min_speech_duration", "min_silence_duration", "prefix_padding_duration", "activation_threshold")),
        ):
            for key in keys:
                if type(table[key]) not in (float, int) or not math.isfinite(table[key]) or table[key] < 0:
                    raise ValueError(f"{key} must be a finite non-negative number.")
        if values["endpoint_max_delay"] < values["endpoint_min_delay"]:
            raise ValueError("endpoint_max_delay must be at least endpoint_min_delay.")
        if values["log_flush_interval_seconds"] <= 0:
            raise ValueError("log_flush_interval_seconds must be positive.")
        if not 0 < vad["activation_threshold"] < 1:
            raise ValueError("activation_threshold must be between zero and one.")
        folder = Path(values["log_directory"])
        return VoiceConfig(
            **{key: value for key, value in values.items() if key != "log_directory"},
            log_directory=folder if folder.is_absolute() else PROJECT_ROOT / folder,
            vad_options=vad,
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid voice configuration at {path}: {error}") from error
