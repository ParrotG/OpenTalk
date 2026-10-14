"""Compose PDF parsing, hierarchical preparation, and vector indexing."""

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from opentalk.documents.config import DocumentConfig, IndexingConfig, load_document_config
from opentalk.documents.embedding import create_embedding_provider
from opentalk.documents.indexing import prepare_index
from opentalk.documents.parser import parse_pdf
from opentalk.documents.vector_config import RagConfig, load_rag_config
from opentalk.documents.vector_index import index_prepared_nodes
from opentalk.documents.vector_store import QdrantVectorStore


@dataclass(frozen=True)
class DocumentPipelineResult:
    document_id: str
    parse_version: str
    parse_path: Path
    index_path: Path
    vector_manifest_path: Path
    collection: str
    point_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            name: str(value) if isinstance(value, Path) else value
            for name, value in asdict(self).items()
        }


def process_pdf(
    input_path: Path,
    *,
    document_config: DocumentConfig | None = None,
    indexing_config: IndexingConfig | None = None,
    rag_config: RagConfig | None = None,
) -> DocumentPipelineResult:
    """Run the three independently testable document stages in data-flow order."""
    document_config = document_config or load_document_config()
    rag_config = rag_config or load_rag_config()
    parsed = parse_pdf(input_path, config=document_config)
    prepared = prepare_index(parsed.parse_path, config=indexing_config)
    provider = create_embedding_provider(rag_config.embedding)
    store = QdrantVectorStore(rag_config.vector_store)
    try:
        indexed = index_prepared_nodes(
            prepared.output_path,
            provider=provider,
            store=store,
            embedding_batch_size=rag_config.embedding.batch_size,
        )
    finally:
        store.close()
    return DocumentPipelineResult(
        document_id=parsed.document_id,
        parse_version=parsed.parse_version,
        parse_path=parsed.parse_path,
        index_path=prepared.output_path,
        vector_manifest_path=indexed.manifest_path,
        collection=indexed.collection,
        point_count=indexed.point_count,
    )
