"""Qdrant storage adapter for prepared document-node embeddings."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opentalk.documents.vector_config import VectorStoreConfig

DENSE_VECTOR_NAME = "dense"


@dataclass(frozen=True)
class VectorPoint:
    point_id: str
    vector: list[float]
    payload: dict[str, Any]


@dataclass(frozen=True)
class VectorSearchHit:
    point_id: str
    score: float
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "point_id": self.point_id,
            "score": self.score,
            "payload": self.payload,
        }


class VectorStore(Protocol):
    store_type: str
    collection_name: str

    def ensure_collection(self, dimension: int) -> None: ...

    def validate_embedding_space(self, *, profile: str, model: str) -> None: ...

    def upsert(self, points: Sequence[VectorPoint]) -> None: ...

    def count(self, filters: Mapping[str, str]) -> int: ...


class QdrantVectorStore:
    """Persist and query named dense vectors in one configured Qdrant collection."""

    store_type = "qdrant"

    def __init__(self, config: VectorStoreConfig, *, client: Any | None = None) -> None:
        self.config = config
        self.collection_name = config.collection
        if client is None:
            try:
                from qdrant_client import QdrantClient
            except ImportError as error:
                raise RuntimeError(
                    "Qdrant client is unavailable. Install the rag dependency group."
                ) from error
            client = QdrantClient(
                url=config.url,
                api_key=os.environ.get(config.api_key_env),
                timeout=config.timeout_seconds,
            )
        self.client = client

    @staticmethod
    def _models() -> Any:
        try:
            from qdrant_client import models
        except ImportError as error:
            raise RuntimeError(
                "Qdrant client is unavailable. Install the rag dependency group."
            ) from error
        return models

    def ensure_collection(self, dimension: int) -> None:
        """Create the collection once, then reject incompatible vector schemas."""
        if dimension <= 0:
            raise ValueError("The dense vector dimension must be positive.")
        models = self._models()
        if not self.client.collection_exists(self.config.collection):
            self.client.create_collection(
                collection_name=self.config.collection,
                vectors_config={
                    DENSE_VECTOR_NAME: models.VectorParams(
                        size=dimension,
                        distance=models.Distance.COSINE,
                    )
                },
            )
            for field_name in (
                "document_id",
                "parse_version",
                "index_profile",
                "embedding_profile",
                "node_type",
            ):
                self.client.create_payload_index(
                    collection_name=self.config.collection,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
            return

        information = self.client.get_collection(self.config.collection)
        vectors = information.config.params.vectors
        if not isinstance(vectors, dict) or DENSE_VECTOR_NAME not in vectors:
            raise ValueError(
                f"Qdrant collection {self.config.collection} lacks the named dense vector."
            )
        dense = vectors[DENSE_VECTOR_NAME]
        if dense.size != dimension or dense.distance != models.Distance.COSINE:
            raise ValueError(
                f"Qdrant collection {self.config.collection} has an incompatible dense schema."
            )

    def validate_embedding_space(self, *, profile: str, model: str) -> None:
        """Prevent a configured collection from silently mixing embedding spaces."""
        records, _ = self.client.scroll(
            collection_name=self.config.collection,
            limit=1,
            with_payload=["embedding_profile", "embedding_model"],
            with_vectors=False,
        )
        if not records:
            return
        payload = records[0].payload or {}
        if payload.get("embedding_profile") != profile or payload.get("embedding_model") != model:
            raise ValueError(
                f"Qdrant collection {self.config.collection} already contains another "
                "embedding space."
            )

    def upsert(self, points: Sequence[VectorPoint]) -> None:
        models = self._models()
        for start in range(0, len(points), self.config.upsert_batch_size):
            batch = points[start : start + self.config.upsert_batch_size]
            self.client.upsert(
                collection_name=self.config.collection,
                points=[
                    models.PointStruct(
                        id=point.point_id,
                        vector={DENSE_VECTOR_NAME: point.vector},
                        payload=point.payload,
                    )
                    for point in batch
                ],
                wait=True,
            )

    def count(self, filters: Mapping[str, str]) -> int:
        models = self._models()
        result = self.client.count(
            collection_name=self.config.collection,
            count_filter=self._filter(filters, models),
            exact=True,
        )
        return int(result.count)

    def search(
        self,
        vector: Sequence[float],
        *,
        limit: int,
        filters: Mapping[str, str] | None = None,
    ) -> list[VectorSearchHit]:
        if limit <= 0:
            raise ValueError("The search limit must be positive.")
        models = self._models()
        result = self.client.query_points(
            collection_name=self.config.collection,
            query=list(vector),
            using=DENSE_VECTOR_NAME,
            query_filter=self._filter(filters or {}, models),
            limit=limit,
            with_payload=True,
            with_vectors=False,
        )
        return [
            VectorSearchHit(
                point_id=str(point.id),
                score=float(point.score),
                payload=dict(point.payload or {}),
            )
            for point in result.points
        ]

    @staticmethod
    def _filter(filters: Mapping[str, str], models: Any) -> Any | None:
        if not filters:
            return None
        return models.Filter(
            must=[
                models.FieldCondition(key=key, match=models.MatchValue(value=value))
                for key, value in filters.items()
            ]
        )

    def close(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
