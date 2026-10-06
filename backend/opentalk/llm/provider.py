"""Create a LiveKit LLM backed by an OpenAI-compatible streaming API."""

import math
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv
from livekit.agents import APIConnectOptions
from livekit.plugins import openai

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    model: str
    api_key_env: str
    temperature: float
    max_completion_tokens: int
    timeout_seconds: float
    max_tool_rounds: int
    extra_body: dict

    @property
    def connection_options(self) -> APIConnectOptions:
        # Tests must report a failed call instead of hiding it behind repeated requests.
        return APIConnectOptions(max_retry=0, timeout=self.timeout_seconds)


def load_llm_config(path: Path | None = None) -> LLMConfig:
    path = path or PROJECT_ROOT / "config/llm.toml"
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)["llm"]
        for key in ("base_url", "model", "api_key_env"):
            if not isinstance(data[key], str) or not data[key].strip():
                raise ValueError(f"{key} must be a non-empty string.")
        endpoint = urlparse(data["base_url"])
        if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
            raise ValueError("base_url must be an HTTP or HTTPS URL.")
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("base_url must not contain credentials, query parameters, or fragments.")
        for key in ("max_completion_tokens", "max_tool_rounds"):
            if type(data[key]) is not int or data[key] <= 0:
                raise ValueError(f"{key} must be a positive integer.")
        for key in ("temperature", "timeout_seconds"):
            if type(data[key]) not in (int, float) or not math.isfinite(data[key]):
                raise ValueError(f"{key} must be a finite number.")
        if not 0 <= data["temperature"] <= 2 or data["timeout_seconds"] <= 0:
            raise ValueError("Temperature or timeout is out of range.")
        extra_body = data.get("extra_body", {})
        if not isinstance(extra_body, dict):
            raise ValueError("extra_body must be a table.")
        reserved = {"model", "messages", "stream", "tools", "tool_choice",
                    "parallel_tool_calls", "max_completion_tokens"}
        if reserved.intersection(extra_body):
            raise ValueError("extra_body must not override protocol or tool settings.")
        return LLMConfig(
            base_url=data["base_url"], model=data["model"], api_key_env=data["api_key_env"],
            temperature=float(data["temperature"]),
            max_completion_tokens=data["max_completion_tokens"],
            timeout_seconds=float(data["timeout_seconds"]),
            max_tool_rounds=data["max_tool_rounds"], extra_body=extra_body,
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid LLM configuration at {path}: {error}") from error


def create_llm(config: LLMConfig | None = None) -> openai.LLM:
    config = config or load_llm_config()
    load_dotenv(PROJECT_ROOT / ".env.local", override=False)
    api_key = os.environ.get(config.api_key_env, "").strip()
    if not api_key:
        raise ValueError(f"Missing required credential: {config.api_key_env}.")
    return openai.LLM(
        base_url=config.base_url,
        model=config.model,
        api_key=api_key,
        temperature=config.temperature,
        max_completion_tokens=config.max_completion_tokens,
        timeout=httpx.Timeout(config.timeout_seconds),
        max_retries=0,
        parallel_tool_calls=False,
        extra_body=config.extra_body,
    )
