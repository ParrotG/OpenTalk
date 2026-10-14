"""Low-level dense vector search used to validate embedding and storage quality."""

import argparse
import json
from pathlib import Path

from opentalk.documents.embedding import create_embedding_provider
from opentalk.documents.vector_config import load_rag_config
from opentalk.documents.vector_store import QdrantVectorStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a raw dense-vector query for embedding-stage inspection."
    )
    parser.add_argument("query", help="Query text to embed and search.")
    parser.add_argument("--config", type=Path, help="Path to a RAG TOML configuration file.")
    parser.add_argument("--limit", type=int, default=10, help="Maximum number of results.")
    parser.add_argument("--document-id", help="Restrict results to one document hash.")
    parser.add_argument("--parse-version", help="Restrict results to one parse version.")
    parser.add_argument("--index-profile", help="Restrict results to one preparation profile.")
    args = parser.parse_args()

    config = load_rag_config(args.config)
    provider = create_embedding_provider(config.embedding)
    store = QdrantVectorStore(config.vector_store)
    filters = {
        key: value
        for key, value in {
            "document_id": args.document_id,
            "parse_version": args.parse_version,
            "index_profile": args.index_profile,
            "embedding_profile": provider.profile,
        }.items()
        if value is not None
    }
    try:
        store.ensure_collection(provider.dimension)
        store.validate_embedding_space(profile=provider.profile, model=provider.model_name)
        hits = store.search(
            provider.embed_query(args.query),
            limit=args.limit,
            filters=filters,
        )
    finally:
        store.close()
    print(json.dumps([hit.to_dict() for hit in hits], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
