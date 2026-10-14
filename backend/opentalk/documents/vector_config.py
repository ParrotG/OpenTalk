"""Load and validate embedding and vector-store configuration."""

import math
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from opentalk.config import PROJECT_ROOT

_PROFILE_PATTERN = re.compile(r"[a-zA-Z0-9._-]{1,100}")
_COLLECTION_PATTERN = re.compile(r"[a-zA-Z0-9._-]{1,255}")


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str
    model: str
    profile: str
    batch_size: int
    max_input_tokens: int


@dataclass(frozen=True)
class VectorStoreConfig:
    url: str
    collection: str
    timeout_seconds: float
    upsert_batch_size: int
    api_key_env: str


@dataclass(frozen=True)
class RetrievalConfig:
    candidate_limit: int
    default_limit: int
    max_limit: int
    max_query_chars: int
    max_context_chars: int
    max_images: int
    max_image_bytes: int


@dataclass(frozen=True)
class RagConfig:
    embedding: EmbeddingConfig
    vector_store: VectorStoreConfig
    retrieval: RetrievalConfig


def load_rag_config(
    path: Path | None = None,
    *,
    environment: Mapping[str, str] | None = None,
) -> RagConfig:
    """Load public RAG settings, with deployment endpoints supplied by the environment."""
    path = path or PROJECT_ROOT / "config/rag.toml"
    environment = environment if environment is not None else os.environ
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)
        embedding = data["embedding"]
        vector_store = data["vector_store"]
        retrieval = data["retrieval"]

        provider = embedding["provider"]
        model = embedding["model"]
        profile = embedding["profile"]
        batch_size = embedding["batch_size"]
        max_input_tokens = embedding["max_input_tokens"]
        if provider != "fastembed":
            raise ValueError("embedding.provider must be fastembed.")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("embedding.model must be a non-empty string.")
        if not isinstance(profile, str) or _PROFILE_PATTERN.fullmatch(profile) is None:
            raise ValueError("embedding.profile contains unsupported characters.")
        if type(batch_size) is not int or batch_size <= 0:
            raise ValueError("embedding.batch_size must be a positive integer.")
        if type(max_input_tokens) is not int or max_input_tokens <= 0:
            raise ValueError("embedding.max_input_tokens must be a positive integer.")

        url = environment.get("OPENTALK_QDRANT_URL", vector_store["url"])
        collection = vector_store["collection"]
        timeout_seconds = vector_store["timeout_seconds"]
        upsert_batch_size = vector_store["upsert_batch_size"]
        api_key_env = vector_store["api_key_env"]
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ValueError("vector_store.url must be an HTTP or HTTPS URL.")
        if (
            not isinstance(collection, str)
            or _COLLECTION_PATTERN.fullmatch(collection) is None
        ):
            raise ValueError("vector_store.collection contains unsupported characters.")
        if (
            type(timeout_seconds) not in (int, float)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("vector_store.timeout_seconds must be a positive finite number.")
        if type(upsert_batch_size) is not int or upsert_batch_size <= 0:
            raise ValueError("vector_store.upsert_batch_size must be a positive integer.")
        if not isinstance(api_key_env, str) or not api_key_env:
            raise ValueError("vector_store.api_key_env must be a non-empty string.")

        retrieval_values = {
            name: retrieval[name]
            for name in (
                "candidate_limit",
                "default_limit",
                "max_limit",
                "max_query_chars",
                "max_context_chars",
                "max_images",
                "max_image_bytes",
            )
        }
        if any(type(value) is not int or value <= 0 for value in retrieval_values.values()):
            raise ValueError("retrieval limits must be positive integers.")
        if retrieval_values["default_limit"] > retrieval_values["max_limit"]:
            raise ValueError("retrieval.default_limit must not exceed retrieval.max_limit.")
        if retrieval_values["candidate_limit"] < retrieval_values["max_limit"]:
            raise ValueError("retrieval.candidate_limit must be at least retrieval.max_limit.")

        return RagConfig(
            embedding=EmbeddingConfig(
                provider=provider,
                model=model.strip(),
                profile=profile,
                batch_size=batch_size,
                max_input_tokens=max_input_tokens,
            ),
            vector_store=VectorStoreConfig(
                url=url,
                collection=collection,
                timeout_seconds=float(timeout_seconds),
                upsert_batch_size=upsert_batch_size,
                api_key_env=api_key_env,
            ),
            retrieval=RetrievalConfig(**retrieval_values),
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid RAG configuration at {path}: {error}") from error
