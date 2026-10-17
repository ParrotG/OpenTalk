"""Qdrant storage adapter for prepared document-node embeddings."""

import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from opentalk.documents.lexical import SparseVector
from opentalk.documents.vector_config import VectorStoreConfig

DENSE_VECTOR_NAME = "dense"
LEXICAL_VECTOR_NAME = "lexical"


@dataclass(frozen=True)
class VectorPoint:
    point_id: str
    vector: list[float]
    payload: dict[str, Any]
    lexical_vector: SparseVector | None = None


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

    def validate_collection(self, dimension: int) -> None: ...

    def validate_embedding_space(self, *, profile: str, model: str) -> None: ...

    def upsert(self, points: Sequence[VectorPoint]) -> None: ...

    def count(self, filters: Mapping[str, str]) -> int: ...

    def delete(self, filters: Mapping[str, str]) -> None: ...

    def hybrid_search(
        self,
        dense_vectors: Sequence[Sequence[float]],
        lexical_vectors: Sequence[SparseVector],
        *,
        candidate_limit: int,
        limit: int,
        filters: Mapping[str, str] | None = None,
    ) -> list[VectorSearchHit]: ...

    def fetch_by_node_ids(
        self,
        node_ids: Sequence[str],
        *,
        filters: Mapping[str, str],
    ) -> list[VectorSearchHit]: ...

    def close(self) -> None: ...


class QdrantVectorStore:
    """Persist and query dense and lexical vectors in one Qdrant collection."""

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
                sparse_vectors_config={
                    LEXICAL_VECTOR_NAME: models.SparseVectorParams(
                        modifier=models.Modifier.IDF,
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

        self.validate_collection(dimension)

    def validate_collection(self, dimension: int) -> None:
        """Require an existing collection with the configured hybrid schema."""
        if dimension <= 0:
            raise ValueError("The dense vector dimension must be positive.")
        models = self._models()
        if not self.client.collection_exists(self.config.collection):
            raise ValueError(f"Qdrant collection {self.config.collection} does not exist.")

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
        sparse_vectors = information.config.params.sparse_vectors
        if not isinstance(sparse_vectors, dict) or LEXICAL_VECTOR_NAME not in sparse_vectors:
            raise ValueError(
                f"Qdrant collection {self.config.collection} lacks the named lexical vector."
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
                        vector={
                            DENSE_VECTOR_NAME: point.vector,
                            **(
                                {
                                    LEXICAL_VECTOR_NAME: models.SparseVector(
                                        indices=point.lexical_vector.indices,
                                        values=point.lexical_vector.values,
                                    )
                                }
                                if point.lexical_vector is not None
                                else {}
                            ),
                        },
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

    def delete(self, filters: Mapping[str, str]) -> None:
        """Delete every point matching the supplied payload fields."""
        if not filters:
            raise ValueError("Vector deletion requires at least one payload filter.")
        if not self.client.collection_exists(self.config.collection):
            return
        models = self._models()
        self.client.delete(
            collection_name=self.config.collection,
            points_selector=models.FilterSelector(
                filter=self._filter(filters, models),
            ),
            wait=True,
        )

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

    def hybrid_search(
        self,
        dense_vectors: Sequence[Sequence[float]],
        lexical_vectors: Sequence[SparseVector],
        *,
        candidate_limit: int,
        limit: int,
        filters: Mapping[str, str] | None = None,
    ) -> list[VectorSearchHit]:
        """Fuse semantic and lexical candidate rankings with reciprocal rank fusion."""
        if not dense_vectors:
            raise ValueError("Hybrid search requires at least one dense query vector.")
        if candidate_limit <= 0 or limit <= 0:
            raise ValueError("Hybrid search limits must be positive.")
        models = self._models()
        prefetch = [
            models.Prefetch(
                query=list(vector),
                using=DENSE_VECTOR_NAME,
                limit=candidate_limit,
            )
            for vector in dense_vectors
        ]
        prefetch.extend(
            models.Prefetch(
                query=models.SparseVector(indices=vector.indices, values=vector.values),
                using=LEXICAL_VECTOR_NAME,
                limit=candidate_limit,
            )
            for vector in lexical_vectors
            if vector.indices
        )
        result = self.client.query_points(
            collection_name=self.config.collection,
            prefetch=prefetch,
            query=models.FusionQuery(fusion=models.Fusion.RRF),
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

    def fetch_by_node_ids(
        self,
        node_ids: Sequence[str],
        *,
        filters: Mapping[str, str],
    ) -> list[VectorSearchHit]:
        if not node_ids:
            return []
        models = self._models()
        conditions = [
            models.FieldCondition(key=key, match=models.MatchValue(value=value))
            for key, value in filters.items()
        ]
        conditions.append(
            models.FieldCondition(
                key="node_id",
                match=models.MatchAny(any=list(dict.fromkeys(node_ids))),
            )
        )
        records, _ = self.client.scroll(
            collection_name=self.config.collection,
            scroll_filter=models.Filter(must=conditions),
            limit=len(set(node_ids)),
            with_payload=True,
            with_vectors=False,
        )
        return [
            VectorSearchHit(
                point_id=str(point.id),
                score=0.0,
                payload=dict(point.payload or {}),
            )
            for point in records
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
