"""Hybrid retrieval and hierarchical evidence expansion for indexed documents."""

import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opentalk.documents.config import load_document_config
from opentalk.documents.embedding import EmbeddingProvider, create_embedding_provider
from opentalk.documents.lexical import encode_lexical
from opentalk.documents.vector_config import RetrievalConfig, load_rag_config
from opentalk.documents.vector_store import QdrantVectorStore, VectorSearchHit, VectorStore


@dataclass(frozen=True)
class RetrievedEvidence:
    rank: int
    score: float
    document_id: str
    parse_version: str
    index_profile: str
    node_id: str
    node_type: str | None
    matched_text: str
    context_text: str
    expansion: str
    parent_node_id: str | None
    page_numbers: list[int]
    section_path: list[str]
    provenance: list[dict[str, Any]]
    asset_references: list[dict[str, Any]]
    image_paths: tuple[Path, ...] = field(repr=False)

    def to_dict(self, *, include_local_paths: bool = False) -> dict[str, Any]:
        value = asdict(self)
        value.pop("image_paths")
        if include_local_paths:
            value["local_image_paths"] = [str(path) for path in self.image_paths]
        return value


@dataclass(frozen=True)
class RetrievalResponse:
    queries: list[str]
    evidence: list[RetrievedEvidence]

    def to_dict(self, *, include_local_paths: bool = False) -> dict[str, Any]:
        return {
            "ok": True,
            "queries": self.queries,
            "result_count": len(self.evidence),
            "evidence": [
                item.to_dict(include_local_paths=include_local_paths) for item in self.evidence
            ],
        }

    def image_paths(self) -> list[Path]:
        paths: list[Path] = []
        seen: set[Path] = set()
        for item in self.evidence:
            for path in item.image_paths:
                if path not in seen:
                    seen.add(path)
                    paths.append(path)
        return paths


class DocumentRetriever(Protocol):
    config: RetrievalConfig

    def search(
        self,
        query: str,
        *,
        alternate_query: str | None = None,
        document_id: str | None = None,
        parse_version: str | None = None,
        index_profile: str | None = None,
        limit: int | None = None,
    ) -> RetrievalResponse: ...

    def close(self) -> None: ...


def _query_variants(query: str, alternate_query: str | None, max_chars: int) -> list[str]:
    variants: list[str] = []
    seen: set[str] = set()
    for raw_query in (query, alternate_query):
        if raw_query is None:
            continue
        if not isinstance(raw_query, str):
            raise ValueError("Retrieval queries must be strings.")
        normalized = " ".join(raw_query.split())
        if not normalized:
            continue
        if len(normalized) > max_chars:
            raise ValueError(f"A retrieval query exceeds the {max_chars}-character limit.")
        key = normalized.casefold()
        if key not in seen:
            seen.add(key)
            variants.append(normalized)
    if not variants:
        raise ValueError("The retrieval query must not be empty.")
    return variants


def _optional_filter(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string when supplied.")
    return value.strip()


def _bounded_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n[Context truncated by retrieval limit.]"
    return text[: limit - len(marker)].rstrip() + marker


class RetrievalService:
    """Retrieve precise leaves, then expand a parent or surrounding indexed leaves."""

    def __init__(
        self,
        *,
        provider: EmbeddingProvider,
        store: VectorStore,
        config: RetrievalConfig,
        storage_root: Path,
    ) -> None:
        self.provider = provider
        self.store = store
        self.config = config
        self.storage_root = storage_root.resolve()
        self._validated = False

    def _validate_store(self) -> None:
        if self._validated:
            return
        self.store.validate_collection(self.provider.dimension)
        self.store.validate_embedding_space(
            profile=self.provider.profile,
            model=self.provider.model_name,
        )
        self._validated = True

    def search(
        self,
        query: str,
        *,
        alternate_query: str | None = None,
        document_id: str | None = None,
        parse_version: str | None = None,
        index_profile: str | None = None,
        limit: int | None = None,
    ) -> RetrievalResponse:
        variants = _query_variants(query, alternate_query, self.config.max_query_chars)
        requested_limit = self.config.default_limit if limit is None else limit
        if type(requested_limit) is not int or not 1 <= requested_limit <= self.config.max_limit:
            raise ValueError(
                f"The retrieval limit must be between 1 and {self.config.max_limit}."
            )
        document_id = _optional_filter("document_id", document_id)
        parse_version = _optional_filter("parse_version", parse_version)
        index_profile = _optional_filter("index_profile", index_profile)
        filters = {"embedding_profile": self.provider.profile}
        filters.update(
            {
                name: value
                for name, value in (
                    ("document_id", document_id),
                    ("parse_version", parse_version),
                    ("index_profile", index_profile),
                )
                if value is not None
            }
        )
        self._validate_store()
        hits = self.store.hybrid_search(
            [self.provider.embed_query(value) for value in variants],
            [encode_lexical(value) for value in variants],
            candidate_limit=self.config.candidate_limit,
            limit=requested_limit,
            filters=filters,
        )
        evidence = [
            self._expand_hit(rank, hit, filters)
            for rank, hit in enumerate(hits, start=1)
        ]
        return RetrievalResponse(queries=variants, evidence=evidence)

    def _expand_hit(
        self,
        rank: int,
        hit: VectorSearchHit,
        base_filters: dict[str, str],
    ) -> RetrievedEvidence:
        payload = hit.payload
        matched_text = str(payload.get("text") or payload.get("embedding_text") or "").strip()
        parent = payload.get("parent")
        parent_text = parent.get("text", "").strip() if isinstance(parent, dict) else ""
        parent_id = parent.get("node_id") if isinstance(parent, dict) else None
        expansion = "parent" if parent_text else "exact"
        context_text = parent_text or matched_text
        if not parent_text:
            ordered_ids = [
                value
                for value in (payload.get("previous_id"), payload.get("next_id"))
                if isinstance(value, str) and value
            ]
            lookup_filters = {
                **base_filters,
                **{
                    name: str(payload[name])
                    for name in ("document_id", "parse_version", "index_profile")
                    if payload.get(name)
                },
            }
            neighbors = {
                str(item.payload.get("node_id")): item.payload
                for item in self.store.fetch_by_node_ids(ordered_ids, filters=lookup_filters)
            }
            previous_id = payload.get("previous_id")
            next_id = payload.get("next_id")
            surrounding = [
                *(
                    [str(neighbors[previous_id].get("text") or "").strip()]
                    if previous_id in neighbors
                    and str(neighbors[previous_id].get("text") or "").strip()
                    else []
                ),
                matched_text,
                *(
                    [str(neighbors[next_id].get("text") or "").strip()]
                    if next_id in neighbors
                    and str(neighbors[next_id].get("text") or "").strip()
                    else []
                ),
            ]
            if len(surrounding) > 1:
                context_text = "\n\n".join(surrounding)
                expansion = "neighbors"

        document_id = str(payload.get("document_id") or "")
        parse_version = str(payload.get("parse_version") or "")
        image_paths = self._resolve_images(
            document_id,
            parse_version,
            payload.get("asset_references"),
        )
        metadata = payload.get("metadata")
        section_path = metadata.get("section_path", []) if isinstance(metadata, dict) else []
        return RetrievedEvidence(
            rank=rank,
            score=hit.score,
            document_id=document_id,
            parse_version=parse_version,
            index_profile=str(payload.get("index_profile") or ""),
            node_id=str(payload.get("node_id") or hit.point_id),
            node_type=(str(payload["node_type"]) if payload.get("node_type") else None),
            matched_text=matched_text,
            context_text=_bounded_text(context_text, self.config.max_context_chars),
            expansion=expansion,
            parent_node_id=str(parent_id) if parent_id else None,
            page_numbers=[int(value) for value in payload.get("page_numbers", [])],
            section_path=[str(value) for value in section_path],
            provenance=[
                dict(value) for value in payload.get("provenance", []) if isinstance(value, dict)
            ],
            asset_references=[
                dict(value)
                for value in payload.get("asset_references", [])
                if isinstance(value, dict)
            ],
            image_paths=tuple(image_paths),
        )

    def _resolve_images(
        self,
        document_id: str,
        parse_version: str,
        asset_references: Any,
    ) -> list[Path]:
        if not document_id or not parse_version or not isinstance(asset_references, list):
            return []
        parse_root = (self.storage_root / document_id / "parses" / parse_version).resolve()
        paths: list[Path] = []
        for reference in asset_references:
            visual = reference.get("visual") if isinstance(reference, dict) else None
            if not isinstance(visual, str) or not visual:
                continue
            path = (parse_root / visual).resolve()
            if path.is_relative_to(parse_root) and path.is_file():
                paths.append(path)
        return paths

    def close(self) -> None:
        self.store.close()


class LazyRetrievalService:
    """Delay model and Qdrant initialization until the agent actually searches."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._service: RetrievalService | None = None
        self._config: RetrievalConfig | None = None

    @property
    def config(self) -> RetrievalConfig:
        with self._lock:
            if self._service is not None:
                return self._service.config
            if self._config is None:
                self._config = load_rag_config().retrieval
            return self._config

    def _get(self) -> RetrievalService:
        with self._lock:
            if self._service is None:
                rag_config = load_rag_config()
                self._config = rag_config.retrieval
                self._service = RetrievalService(
                    provider=create_embedding_provider(rag_config.embedding),
                    store=QdrantVectorStore(rag_config.vector_store),
                    config=rag_config.retrieval,
                    storage_root=load_document_config().storage_root,
                )
            return self._service

    def search(self, query: str, **options: Any) -> RetrievalResponse:
        return self._get().search(query, **options)

    def close(self) -> None:
        with self._lock:
            service, self._service = self._service, None
        if service is not None:
            service.close()
