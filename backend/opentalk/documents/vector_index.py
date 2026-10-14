"""Embed prepared document nodes and persist them in a vector store."""

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentalk.documents.embedding import EmbeddingProvider, validate_vectors
from opentalk.documents.lexical import encode_lexical
from opentalk.documents.vector_store import VectorPoint, VectorStore


@dataclass(frozen=True)
class VectorIndexResult:
    document_id: str
    parse_version: str
    index_profile: str
    embedding_profile: str
    collection: str
    point_count: int
    manifest_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in asdict(self).items()
        }


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _load_prepared_index(index_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    index_manifest_path = index_path / "index_manifest.json"
    nodes_path = index_path / "nodes.jsonl"
    if not index_manifest_path.is_file() or not nodes_path.is_file():
        raise ValueError(
            "The indexing directory must contain index_manifest.json and nodes.jsonl."
        )
    manifest = json.loads(index_manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed":
        raise ValueError("Vector indexing requires a completed index-preparation manifest.")
    nodes: list[dict[str, Any]] = []
    identifiers: set[str] = set()
    for line_number, line in enumerate(nodes_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            node = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid nodes.jsonl record at line {line_number}.") from error
        node_id = node.get("node_id") if isinstance(node, dict) else None
        if not isinstance(node_id, str) or node_id in identifiers:
            raise ValueError(f"Invalid or duplicate node ID at line {line_number}.")
        identifiers.add(node_id)
        nodes.append(node)
    return manifest, nodes


def _point_id(
    document_id: str,
    parse_version: str,
    index_profile: str,
    embedding_profile: str,
    node_id: str,
) -> str:
    value = "\0".join(
        (document_id, parse_version, index_profile, embedding_profile, node_id)
    )
    return str(uuid.uuid5(uuid.NAMESPACE_URL, value))


def _parent_payload(parent: dict[str, Any] | None) -> dict[str, Any] | None:
    if parent is None:
        return None
    return {
        "node_id": parent["node_id"],
        "node_type": parent.get("node_type"),
        "text": parent.get("text", ""),
        "element_ids": parent.get("element_ids", []),
        "page_numbers": parent.get("page_numbers", []),
        "provenance": parent.get("provenance", []),
        "asset_references": parent.get("asset_references", []),
        "metadata": parent.get("metadata", {}),
    }


def _payload(
    node: dict[str, Any],
    *,
    parent: dict[str, Any] | None,
    document_id: str,
    parse_version: str,
    index_profile: str,
    provider: EmbeddingProvider,
) -> dict[str, Any]:
    embedding_text = node["embedding_text"].strip()
    return {
        "schema_version": 1,
        "document_id": document_id,
        "parse_version": parse_version,
        "index_profile": index_profile,
        "embedding_profile": provider.profile,
        "embedding_provider": provider.provider_name,
        "embedding_model": provider.model_name,
        "node_id": node["node_id"],
        "node_type": node.get("node_type"),
        "parent_id": node.get("parent_id"),
        "previous_id": node.get("previous_id"),
        "next_id": node.get("next_id"),
        "text": node.get("text", ""),
        "embedding_text": embedding_text,
        "sparse_text": node.get("sparse_text", embedding_text),
        "content_sha256": hashlib.sha256(embedding_text.encode()).hexdigest(),
        "element_ids": node.get("element_ids", []),
        "page_numbers": node.get("page_numbers", []),
        "provenance": node.get("provenance", []),
        "asset_references": node.get("asset_references", []),
        "metadata": node.get("metadata", {}),
        "parent": _parent_payload(parent),
    }


def _point_filters(
    *,
    document_id: str,
    parse_version: str,
    index_profile: str,
    embedding_profile: str,
) -> dict[str, str]:
    return {
        "document_id": document_id,
        "parse_version": parse_version,
        "index_profile": index_profile,
        "embedding_profile": embedding_profile,
    }


def index_prepared_nodes(
    index_path: Path,
    *,
    provider: EmbeddingProvider,
    store: VectorStore,
    embedding_batch_size: int,
) -> VectorIndexResult:
    """Embed every indexable node and idempotently upsert its vector and retrieval payload."""
    if embedding_batch_size <= 0:
        raise ValueError("The embedding batch size must be positive.")
    index_path = index_path.resolve()
    source_manifest, nodes = _load_prepared_index(index_path)
    document_id = source_manifest.get("document_id")
    parse_version = source_manifest.get("parse_version")
    index_profile = source_manifest.get("profile")
    if not all(
        isinstance(value, str) and value
        for value in (document_id, parse_version, index_profile)
    ):
        raise ValueError(
            "The index-preparation manifest is missing document_id, parse_version, or profile."
        )
    if re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", provider.profile) is None:
        raise ValueError("The embedding profile contains unsupported characters.")
    node_ids = {node["node_id"] for node in nodes}
    indexable = [node for node in nodes if node.get("indexable") is True]
    if not indexable:
        raise ValueError("The prepared index has no indexable nodes.")
    for node in indexable:
        if not isinstance(node.get("embedding_text"), str) or not node["embedding_text"].strip():
            raise ValueError(f"Indexable node {node['node_id']} has empty embedding text.")
        parent_id = node.get("parent_id")
        if not isinstance(parent_id, str) or parent_id not in node_ids:
            raise ValueError(f"Indexable node {node['node_id']} has an invalid parent reference.")

    manifest_path = index_path / "vector_indexes" / provider.profile / "manifest.json"
    started_at = datetime.now(UTC)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "indexing",
        "started_at": started_at.isoformat(),
        "document_id": document_id,
        "parse_version": parse_version,
        "index_profile": index_profile,
        "embedding": {
            "provider": provider.provider_name,
            "model": provider.model_name,
            "profile": provider.profile,
            "dimension": provider.dimension,
        },
        "vector_store": {
            "type": store.store_type,
            "collection": store.collection_name,
            "vector_name": "dense",
            "lexical_vector_name": "lexical",
        },
        "source": {"index_manifest": "../../index_manifest.json", "nodes": "../../nodes.jsonl"},
    }
    _write_json(manifest_path, manifest)
    nodes_by_id = {node["node_id"]: node for node in nodes}
    try:
        store.ensure_collection(provider.dimension)
        store.validate_embedding_space(profile=provider.profile, model=provider.model_name)
        for start in range(0, len(indexable), embedding_batch_size):
            batch = indexable[start : start + embedding_batch_size]
            texts = [node["embedding_text"].strip() for node in batch]
            try:
                vectors = validate_vectors(
                    provider.embed_documents(texts),
                    expected_count=len(batch),
                    expected_dimension=provider.dimension,
                )
            except ValueError as error:
                node_ids = ", ".join(node["node_id"] for node in batch)
                raise ValueError(f"Embedding failed for nodes {node_ids}: {error}") from error
            points = []
            for node, vector in zip(batch, vectors, strict=True):
                parent_id = node.get("parent_id")
                parent = nodes_by_id.get(parent_id) if isinstance(parent_id, str) else None
                lexical_vector = encode_lexical(node.get("sparse_text", ""))
                points.append(
                    VectorPoint(
                        point_id=_point_id(
                            document_id,
                            parse_version,
                            index_profile,
                            provider.profile,
                            node["node_id"],
                        ),
                        vector=vector,
                        lexical_vector=lexical_vector if lexical_vector.indices else None,
                        payload=_payload(
                            node,
                            parent=parent,
                            document_id=document_id,
                            parse_version=parse_version,
                            index_profile=index_profile,
                            provider=provider,
                        ),
                    )
                )
            store.upsert(points)

        point_count = store.count(
            _point_filters(
                document_id=document_id,
                parse_version=parse_version,
                index_profile=index_profile,
                embedding_profile=provider.profile,
            )
        )
        if point_count != len(indexable):
            raise RuntimeError(
                f"Qdrant contains {point_count} matching points; expected {len(indexable)}."
            )
        manifest.update(
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            point_count=point_count,
        )
        _write_json(manifest_path, manifest)
    except Exception as error:
        manifest.update(
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error={"type": type(error).__name__, "message": str(error)},
        )
        _write_json(manifest_path, manifest)
        raise

    return VectorIndexResult(
        document_id=document_id,
        parse_version=parse_version,
        index_profile=index_profile,
        embedding_profile=provider.profile,
        collection=store.collection_name,
        point_count=point_count,
        manifest_path=manifest_path,
    )
