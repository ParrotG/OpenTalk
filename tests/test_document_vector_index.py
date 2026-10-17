import json
from pathlib import Path

import pytest

from opentalk.documents.embedding import FastEmbedProvider, validate_vectors
from opentalk.documents.lexical import encode_lexical
from opentalk.documents.vector_config import (
    EmbeddingConfig,
    VectorStoreConfig,
    load_rag_config,
)
from opentalk.documents.vector_index import index_prepared_nodes
from opentalk.documents.vector_store import QdrantVectorStore, VectorPoint


class FakeEmbeddingProvider:
    provider_name = "fake"
    model_name = "fake-multilingual"
    profile = "fake-v1"
    dimension = 3

    @staticmethod
    def _vector(text: str) -> list[float]:
        lowered = text.lower()
        return [
            float(lowered.count("alpha")),
            float(lowered.count("diagram")),
            1.0,
        ]

    def embed_documents(self, texts):
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)


class FakeFastEmbedModel:
    embedding_size = 2

    def __init__(self):
        self.calls = []

    @staticmethod
    def token_count(text):
        return len(text.split())

    def passage_embed(self, texts, *, batch_size):
        self.calls.append(("passage", texts, batch_size))
        return iter(([1.0, 0.0] for _ in texts))

    def query_embed(self, texts, *, batch_size):
        self.calls.append(("query", texts, batch_size))
        return iter(([0.0, 1.0] for _ in texts))


def store_configuration(collection: str = "documents_test") -> VectorStoreConfig:
    return VectorStoreConfig(
        url="http://127.0.0.1:6333",
        collection=collection,
        timeout_seconds=5.0,
        upsert_batch_size=1,
        api_key_env="QDRANT_API_KEY",
    )


def write_prepared_index(tmp_path: Path) -> Path:
    index_path = tmp_path / "indexing" / "hierarchical-v2"
    index_path.mkdir(parents=True)
    (index_path / "index_manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "document_id": "document-1",
                "parse_version": "parse-1",
                "profile": "hierarchical-v2",
            }
        ),
        encoding="utf-8",
    )
    nodes = [
        {
            "node_id": "document",
            "node_type": "document",
            "parent_id": None,
            "indexable": False,
            "text": "fixture.pdf",
        },
        {
            "node_id": "context",
            "node_type": "context",
            "parent_id": "document",
            "indexable": False,
            "text": "Alpha explanation. Architecture diagram.",
            "element_ids": ["#/texts/0", "#/pictures/0"],
            "page_numbers": [1, 2],
            "provenance": [],
            "asset_references": [{"visual": "visuals/picture.png"}],
            "metadata": {"section_path": ["Overview"]},
        },
        {
            "node_id": "chunk-alpha",
            "node_type": "chunk",
            "parent_id": "context",
            "previous_id": None,
            "next_id": "picture",
            "indexable": True,
            "text": "Alpha explanation.",
            "embedding_text": "Overview\nAlpha explanation.",
            "sparse_text": "Overview\nAlpha explanation.",
            "element_ids": ["#/texts/0"],
            "page_numbers": [1],
            "provenance": [{"element_id": "#/texts/0", "page_no": 1}],
            "asset_references": [],
            "metadata": {"section_path": ["Overview"]},
        },
        {
            "node_id": "picture",
            "node_type": "picture",
            "parent_id": "context",
            "previous_id": "chunk-alpha",
            "next_id": None,
            "indexable": True,
            "text": "Architecture diagram.",
            "embedding_text": "Overview\nArchitecture diagram.",
            "sparse_text": "Overview\nArchitecture diagram.",
            "element_ids": ["#/pictures/0"],
            "page_numbers": [2],
            "provenance": [{"element_id": "#/pictures/0", "page_no": 2}],
            "asset_references": [{"visual": "visuals/picture.png", "table": None}],
            "metadata": {"section_path": ["Overview"], "label": "picture"},
        },
    ]
    (index_path / "nodes.jsonl").write_text(
        "\n".join(json.dumps(node) for node in nodes) + "\n",
        encoding="utf-8",
    )
    return index_path


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_vector_index_upserts_only_indexable_nodes_and_preserves_retrieval_payload(tmp_path):
    from qdrant_client import QdrantClient

    index_path = write_prepared_index(tmp_path)
    provider = FakeEmbeddingProvider()
    store = QdrantVectorStore(
        store_configuration(),
        client=QdrantClient(":memory:"),
    )

    first = index_prepared_nodes(
        index_path,
        provider=provider,
        store=store,
        embedding_batch_size=1,
    )
    second = index_prepared_nodes(
        index_path,
        provider=provider,
        store=store,
        embedding_batch_size=2,
    )

    assert first.point_count == second.point_count == 2
    assert store.count({"embedding_profile": "fake-v1"}) == 2
    hits = store.search(
        provider.embed_query("alpha"),
        limit=2,
        filters={"document_id": "document-1", "embedding_profile": "fake-v1"},
    )
    assert hits[0].payload["node_id"] == "chunk-alpha"
    assert hits[0].payload["parent"]["node_id"] == "context"
    picture = next(hit for hit in hits if hit.payload["node_id"] == "picture")
    assert picture.payload["asset_references"][0]["visual"] == "visuals/picture.png"
    assert picture.payload["page_numbers"] == [2]
    assert picture.payload["sparse_text"] == "Overview\nArchitecture diagram."
    hybrid_hits = store.hybrid_search(
        [provider.embed_query("diagram")],
        [encode_lexical("diagram")],
        candidate_limit=10,
        limit=2,
        filters={"document_id": "document-1", "embedding_profile": "fake-v1"},
    )
    assert hybrid_hits[0].payload["node_id"] == "picture"
    fetched = store.fetch_by_node_ids(
        ["picture"],
        filters={"document_id": "document-1", "embedding_profile": "fake-v1"},
    )
    assert [item.payload["node_id"] for item in fetched] == ["picture"]

    manifest = json.loads(first.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["point_count"] == 2
    assert manifest["vector_store"]["collection"] == "documents_test"
    assert manifest["vector_store"]["lexical_vector_name"] == "lexical"
    store.delete({"document_id": "document-1"})
    assert store.count({"document_id": "document-1"}) == 0
    store.close()


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_qdrant_store_rejects_an_incompatible_collection_schema():
    from qdrant_client import QdrantClient

    client = QdrantClient(":memory:")
    store = QdrantVectorStore(store_configuration(), client=client)
    store.ensure_collection(3)

    with pytest.raises(ValueError, match="incompatible dense schema"):
        store.ensure_collection(2)
    store.close()


def test_qdrant_store_rejects_a_dense_only_collection():
    from qdrant_client import QdrantClient, models

    client = QdrantClient(":memory:")
    client.create_collection(
        collection_name="dense_only",
        vectors_config={
            "dense": models.VectorParams(size=3, distance=models.Distance.COSINE)
        },
    )
    store = QdrantVectorStore(store_configuration("dense_only"), client=client)

    with pytest.raises(ValueError, match="lacks the named lexical vector"):
        store.ensure_collection(3)
    store.close()


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_qdrant_store_rejects_mixed_embedding_spaces():
    from qdrant_client import QdrantClient

    store = QdrantVectorStore(
        store_configuration("embedding_space_test"),
        client=QdrantClient(":memory:"),
    )
    store.ensure_collection(3)
    store.upsert(
        [
            VectorPoint(
                point_id="d8adc21f-cbf2-432b-9ae8-339afa22c394",
                vector=[1.0, 0.0, 0.0],
                payload={"embedding_profile": "profile-a", "embedding_model": "model-a"},
            )
        ]
    )

    store.validate_embedding_space(profile="profile-a", model="model-a")
    with pytest.raises(ValueError, match="another embedding space"):
        store.validate_embedding_space(profile="profile-b", model="model-b")
    store.close()


@pytest.mark.filterwarnings("ignore:Payload indexes have no effect in the local Qdrant")
def test_vector_index_records_storage_failure_without_reporting_completion(tmp_path):
    from qdrant_client import QdrantClient

    index_path = write_prepared_index(tmp_path)
    store = QdrantVectorStore(
        store_configuration("failed_index_test"),
        client=QdrantClient(":memory:"),
    )
    store.ensure_collection(2)

    with pytest.raises(ValueError, match="incompatible dense schema"):
        index_prepared_nodes(
            index_path,
            provider=FakeEmbeddingProvider(),
            store=store,
            embedding_batch_size=2,
        )

    manifest_path = index_path / "vector_indexes" / "fake-v1" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "ValueError"
    assert "incompatible dense schema" in manifest["error"]["message"]
    store.close()


def test_fastembed_provider_uses_distinct_passage_and_query_paths():
    model = FakeFastEmbedModel()
    provider = FastEmbedProvider(
        EmbeddingConfig(
            provider="fastembed",
            model="fixture-model",
            profile="fixture-v1",
            batch_size=4,
            max_input_tokens=8,
        ),
        model=model,
    )

    assert provider.embed_documents(["one", "two"]) == [[1.0, 0.0], [1.0, 0.0]]
    assert provider.embed_query("question") == [0.0, 1.0]
    assert model.calls == [
        ("passage", ["one", "two"], 4),
        ("query", ["question"], 1),
    ]

    with pytest.raises(ValueError, match="configured limit"):
        provider.embed_query("one two three four five six seven eight nine")


def test_vector_validation_rejects_invalid_provider_output():
    with pytest.raises(ValueError, match="dimension"):
        validate_vectors([[1.0]], expected_count=1, expected_dimension=2)
    with pytest.raises(ValueError, match="non-finite"):
        validate_vectors([[1.0, float("nan")]], expected_count=1, expected_dimension=2)


def test_load_rag_config_validates_values_and_allows_endpoint_override(tmp_path):
    path = tmp_path / "rag.toml"
    path.write_text(
        """[embedding]
provider = "fastembed"
model = "fixture-model"
profile = "fixture-v1"
batch_size = 8
max_input_tokens = 512

[vector_store]
url = "http://127.0.0.1:6333"
collection = "fixture_collection"
timeout_seconds = 4.0
upsert_batch_size = 16
api_key_env = "QDRANT_API_KEY"

[retrieval]
candidate_limit = 20
default_limit = 5
max_limit = 10
max_query_chars = 1000
max_context_chars = 5000
max_images = 2
max_image_bytes = 8388608
""",
        encoding="utf-8",
    )

    config = load_rag_config(
        path,
        environment={"OPENTALK_QDRANT_URL": "http://qdrant:6333"},
    )

    assert config.embedding.profile == "fixture-v1"
    assert config.vector_store.url == "http://qdrant:6333"
    assert config.vector_store.upsert_batch_size == 16
    assert config.retrieval.default_limit == 5

    path.write_text(
        path.read_text(encoding="utf-8").replace("batch_size = 8", "batch_size = 0"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="batch_size"):
        load_rag_config(path, environment={})
