"""Configure the official Soniox plugin without application-level language state."""

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from dotenv import load_dotenv
from livekit.agents import APIConnectOptions
from livekit.plugins import soniox

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class ASRConfig:
    base_url: str
    model: str
    api_key_env: str
    sample_rate: int
    language_hints: tuple[str, ...]
    max_endpoint_delay_ms: int
    connection_timeout_seconds: float
    frame_ms: int
    tail_silence_ms: int
    drain_timeout_seconds: float

    @property
    def connection_options(self) -> APIConnectOptions:
        return APIConnectOptions(max_retry=0, timeout=self.connection_timeout_seconds)


def load_asr_config(path: Path | None = None) -> ASRConfig:
    path = path or PROJECT_ROOT / "config/asr.toml"
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        data = document["asr"]
        replay = document["replay"]
        for key in ("base_url", "model", "api_key_env"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError(f"{key} must be a non-empty string.")
        endpoint = urlparse(data["base_url"])
        if endpoint.scheme != "wss" or not endpoint.netloc:
            raise ValueError("base_url must be a secure WebSocket URL.")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("base_url must not contain credentials, query parameters, or fragments.")
        sample_rate = data["sample_rate"]
        if type(sample_rate) is not int or sample_rate not in (8000, 16000, 24000, 48000):
            raise ValueError("sample_rate must be 8000, 16000, 24000, or 48000.")
        hints = data["language_hints"]
        if not isinstance(hints, list) or any(
            not isinstance(hint, str) or not hint.strip() for hint in hints
        ) or len(set(hints)) != len(hints):
            raise ValueError("language_hints must contain unique non-empty language codes.")
        endpoint_ms = data["max_endpoint_delay_ms"]
        if type(endpoint_ms) is not int or not 500 <= endpoint_ms <= 3000:
            raise ValueError("max_endpoint_delay_ms must be between 500 and 3000.")
        frame_ms = replay["frame_ms"]
        tail_ms = replay["tail_silence_ms"]
        if type(frame_ms) is not int or not 10 <= frame_ms <= 100:
            raise ValueError("frame_ms must be between 10 and 100.")
        if type(tail_ms) is not int or not endpoint_ms + 500 <= tail_ms <= 10000:
            raise ValueError("tail_silence_ms must exceed endpoint delay by at least 500 ms and be at most 10000.")
        for values, key in ((data, "connection_timeout_seconds"), (replay, "drain_timeout_seconds")):
            if type(values[key]) not in (int, float) or not math.isfinite(values[key]) or values[key] <= 0:
                raise ValueError(f"{key} must be a finite positive number.")
        return ASRConfig(
            base_url=data["base_url"], model=data["model"], api_key_env=data["api_key_env"],
            sample_rate=sample_rate, language_hints=tuple(hints),
            max_endpoint_delay_ms=endpoint_ms,
            connection_timeout_seconds=float(data["connection_timeout_seconds"]),
            frame_ms=frame_ms, tail_silence_ms=tail_ms,
            drain_timeout_seconds=float(replay["drain_timeout_seconds"]),
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid ASR configuration at {path}: {error}") from error


def create_stt(
    config: ASRConfig | None = None, *, http_session: aiohttp.ClientSession | None = None,
) -> soniox.STT:
    config = config or load_asr_config()
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"Missing required credential: {config.api_key_env}.")
    return soniox.STT(
        api_key=api_key, base_url=config.base_url, http_session=http_session,
        params=soniox.STTOptions(
            model=config.model, sample_rate=config.sample_rate, num_channels=1,
            language_hints=list(config.language_hints) or None,
            language_hints_strict=False,
            enable_language_identification=False,
            enable_speaker_diarization=False,
            max_endpoint_delay_ms=config.max_endpoint_delay_ms,
        ),
    )
