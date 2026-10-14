"""Command-line entry point for embedding and storing prepared document nodes."""

import argparse
import json
from pathlib import Path

from opentalk.documents.embedding import create_embedding_provider
from opentalk.documents.vector_config import load_rag_config
from opentalk.documents.vector_index import index_prepared_nodes
from opentalk.documents.vector_store import QdrantVectorStore


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Embed prepared document nodes and upsert them into Qdrant."
    )
    parser.add_argument("index_path", type=Path, help="Path to an indexing/<profile> directory.")
    parser.add_argument("--config", type=Path, help="Path to a RAG TOML configuration file.")
    args = parser.parse_args()

    config = load_rag_config(args.config)
    provider = create_embedding_provider(config.embedding)
    store = QdrantVectorStore(config.vector_store)
    try:
        result = index_prepared_nodes(
            args.index_path,
            provider=provider,
            store=store,
            embedding_batch_size=config.embedding.batch_size,
        )
    finally:
        store.close()
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
