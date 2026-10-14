"""Embedding provider contracts and the local FastEmbed implementation."""

import math
from collections.abc import Sequence
from typing import Any, Protocol

from opentalk.documents.vector_config import EmbeddingConfig


class EmbeddingProvider(Protocol):
    provider_name: str
    model_name: str
    profile: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


def validate_vectors(
    vectors: Sequence[Sequence[float]],
    *,
    expected_count: int,
    expected_dimension: int,
) -> list[list[float]]:
    """Validate provider output before any vector is sent to storage."""
    if len(vectors) != expected_count:
        raise ValueError(
            f"Embedding provider returned {len(vectors)} vectors for {expected_count} texts."
        )
    validated: list[list[float]] = []
    for position, vector in enumerate(vectors):
        values = [float(value) for value in vector]
        if len(values) != expected_dimension:
            raise ValueError(
                f"Embedding vector {position} has dimension {len(values)}; "
                f"expected {expected_dimension}."
            )
        if any(not math.isfinite(value) for value in values):
            raise ValueError(f"Embedding vector {position} contains a non-finite value.")
        validated.append(values)
    return validated


class FastEmbedProvider:
    """Generate local CPU embeddings while keeping the model outside the realtime runtime."""

    provider_name = "fastembed"

    def __init__(self, config: EmbeddingConfig, *, model: Any | None = None) -> None:
        if model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as error:
                raise RuntimeError(
                    "FastEmbed is unavailable. Install the rag dependency group."
                ) from error
            model = TextEmbedding(model_name=config.model)
        self._model = model
        self.model_name = config.model
        self.profile = config.profile
        self.batch_size = config.batch_size
        self.max_input_tokens = config.max_input_tokens
        self.dimension = int(model.embedding_size)

    def _validate_token_limits(self, texts: Sequence[str]) -> None:
        for position, text in enumerate(texts):
            token_count = int(self._model.token_count(text))
            if token_count > self.max_input_tokens:
                raise ValueError(
                    f"Embedding text {position} contains {token_count} tokens; "
                    f"the configured limit is {self.max_input_tokens}. Prepare smaller chunks "
                    "instead of truncating content."
                )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("Embedding documents must be non-empty strings.")
        self._validate_token_limits(texts)
        vectors = list(self._model.passage_embed(list(texts), batch_size=self.batch_size))
        return validate_vectors(
            vectors,
            expected_count=len(texts),
            expected_dimension=self.dimension,
        )

    def embed_query(self, text: str) -> list[float]:
        if not isinstance(text, str) or not text.strip():
            raise ValueError("The embedding query must be a non-empty string.")
        self._validate_token_limits([text])
        vectors = list(self._model.query_embed([text], batch_size=1))
        return validate_vectors(
            vectors,
            expected_count=1,
            expected_dimension=self.dimension,
        )[0]


def create_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    if config.provider == "fastembed":
        return FastEmbedProvider(config)
    raise ValueError(f"Unsupported embedding provider: {config.provider}")
