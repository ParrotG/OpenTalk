"""Create the native Soniox TTS with public configuration and private credentials."""

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
class TTSConfig:
    websocket_url: str
    model: str
    voice: str
    language: str
    api_key_env: str
    sample_rate: int
    speed: float
    connection_timeout_seconds: float
    stream_idle_timeout_seconds: float
    chunk_chars: int
    chunk_delay_ms: int
    timeout_seconds: float

    @property
    def connection_options(self) -> APIConnectOptions:
        return APIConnectOptions(max_retry=0, timeout=self.connection_timeout_seconds)


def load_tts_config(path: Path | None = None) -> TTSConfig:
    path = path or PROJECT_ROOT / "config/tts.toml"
    try:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
        data = document["tts"]
        smoke = document["smoke"]
        for key in ("websocket_url", "model", "voice", "language", "api_key_env"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError(f"{key} must be a non-empty string.")
        endpoint = urlparse(data["websocket_url"])
        if endpoint.scheme != "wss" or not endpoint.netloc:
            raise ValueError("websocket_url must be a secure WebSocket URL.")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("websocket_url must not contain credentials, query parameters, or fragments.")
        if type(data["sample_rate"]) is not int or data["sample_rate"] not in (8000, 16000, 24000, 44100, 48000):
            raise ValueError("sample_rate must be 8000, 16000, 24000, 44100, or 48000.")
        for values, key in (
            (data, "speed"), (data, "connection_timeout_seconds"),
            (data, "stream_idle_timeout_seconds"), (smoke, "timeout_seconds"),
        ):
            if type(values[key]) not in (int, float) or not math.isfinite(values[key]) or values[key] <= 0:
                raise ValueError(f"{key} must be a finite positive number.")
        if not 0.7 <= data["speed"] <= 1.3:
            raise ValueError("speed must be between 0.7 and 1.3.")
        if type(smoke["chunk_chars"]) is not int or smoke["chunk_chars"] <= 0:
            raise ValueError("chunk_chars must be a positive integer.")
        if type(smoke["chunk_delay_ms"]) is not int or smoke["chunk_delay_ms"] < 0:
            raise ValueError("chunk_delay_ms must be a non-negative integer.")
        return TTSConfig(**data, **smoke)
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid TTS configuration at {path}: {error}") from error


def create_tts(
    config: TTSConfig | None = None, *, http_session: aiohttp.ClientSession | None = None,
) -> soniox.TTS:
    config = config or load_tts_config()
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"Missing required credential: {config.api_key_env}.")
    return soniox.TTS(
        api_key=api_key, http_session=http_session, websocket_url=config.websocket_url,
        model=config.model, voice=config.voice, language=config.language,
        audio_format="pcm_s16le", sample_rate=config.sample_rate, speed=config.speed,
        stream_idle_timeout=config.stream_idle_timeout_seconds,
    )
