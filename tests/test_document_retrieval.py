import pytest

from opentalk.documents.lexical import SparseVector, encode_lexical, lexical_tokens
from opentalk.documents.retrieval import RetrievalService
from opentalk.documents.vector_config import RetrievalConfig
from opentalk.documents.vector_store import VectorSearchHit


class FakeEmbeddingProvider:
    provider_name = "fake"
    model_name = "fake-model"
    profile = "fake-v1"
    dimension = 3

    def __init__(self):
        self.queries = []

    def embed_query(self, text):
        self.queries.append(text)
        return [1.0, 0.0, 0.0]

    def embed_documents(self, texts):
        return [[1.0, 0.0, 0.0] for _ in texts]


class FakeVectorStore:
    store_type = "fake"
    collection_name = "fixture"

    def __init__(self, hit, neighbors=()):
        self.hit = hit
        self.neighbors = list(neighbors)
        self.hybrid_call = None
        self.fetch_call = None
        self.closed = False

    def validate_collection(self, dimension):
        assert dimension == 3

    def validate_embedding_space(self, *, profile, model):
        assert profile == "fake-v1" and model == "fake-model"

    def hybrid_search(
        self,
        dense_vectors,
        lexical_vectors,
        *,
        candidate_limit,
        limit,
        filters=None,
    ):
        self.hybrid_call = {
            "dense_vectors": dense_vectors,
            "lexical_vectors": lexical_vectors,
            "candidate_limit": candidate_limit,
            "limit": limit,
            "filters": filters,
        }
        return [self.hit]

    def fetch_by_node_ids(self, node_ids, *, filters):
        self.fetch_call = {"node_ids": node_ids, "filters": filters}
        return self.neighbors

    def close(self):
        self.closed = True


def retrieval_config() -> RetrievalConfig:
    return RetrievalConfig(
        candidate_limit=20,
        default_limit=5,
        max_limit=10,
        max_query_chars=100,
        max_context_chars=500,
        max_images=2,
        max_image_bytes=1024,
    )


def payload(**updates):
    value = {
        "document_id": "document-1",
        "parse_version": "parse-1",
        "index_profile": "hierarchical-v2",
        "embedding_profile": "fake-v1",
        "node_id": "leaf-1",
        "node_type": "picture",
        "text": "Precise architecture diagram description.",
        "previous_id": None,
        "next_id": None,
        "page_numbers": [3],
        "provenance": [{"page_no": 3}],
        "asset_references": [{"visual": "visuals/architecture.png"}],
        "metadata": {"section_path": ["Architecture"]},
        "parent": {
            "node_id": "context-1",
            "text": "Expanded architecture context.",
        },
    }
    value.update(updates)
    return value


def test_lexical_encoder_handles_chinese_terms_and_exact_identifiers():
    tokens = lexical_tokens("检索架构 API-204")
    assert "检索" in tokens
    assert "架构" in tokens
    assert "api" in tokens
    assert "204" in tokens
    first = encode_lexical("检索架构 API-204")
    second = encode_lexical("检索架构 API-204")
    assert first == second
    assert first.indices == sorted(first.indices)
    assert all(value > 0 for value in first.values)


def test_retrieval_fuses_query_variants_and_expands_parent_with_image(tmp_path):
    image = tmp_path / "document-1" / "parses" / "parse-1" / "visuals" / "architecture.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"fixture")
    provider = FakeEmbeddingProvider()
    store = FakeVectorStore(
        VectorSearchHit(point_id="point-1", score=0.8, payload=payload())
    )
    service = RetrievalService(
        provider=provider,
        store=store,
        config=retrieval_config(),
        storage_root=tmp_path,
    )

    response = service.search(
        "How does retrieval work?",
        alternate_query="检索架构",
        document_id="document-1",
        limit=3,
    )

    assert response.queries == ["How does retrieval work?", "检索架构"]
    assert provider.queries == response.queries
    assert store.hybrid_call["candidate_limit"] == 20
    assert all(isinstance(vector, SparseVector) for vector in store.hybrid_call["lexical_vectors"])
    assert store.hybrid_call["filters"] == {
        "embedding_profile": "fake-v1",
        "document_id": "document-1",
    }
    evidence = response.evidence[0]
    assert evidence.expansion == "parent"
    assert evidence.context_text == "Expanded architecture context."
    assert evidence.page_numbers == [3]
    assert evidence.image_paths == (image,)
    assert "local_image_paths" not in response.to_dict()["evidence"][0]


def test_retrieval_falls_back_to_surrounding_leaves_when_parent_is_missing(tmp_path):
    hit = VectorSearchHit(
        point_id="point-1",
        score=0.7,
        payload=payload(
            node_type="chunk",
            text="Matched paragraph.",
            parent=None,
            previous_id="previous",
            next_id="next",
            asset_references=[],
        ),
    )
    neighbors = [
        VectorSearchHit("point-2", 0.0, {"node_id": "previous", "text": "Previous paragraph."}),
        VectorSearchHit("point-3", 0.0, {"node_id": "next", "text": "Next paragraph."}),
    ]
    store = FakeVectorStore(hit, neighbors)
    service = RetrievalService(
        provider=FakeEmbeddingProvider(),
        store=store,
        config=retrieval_config(),
        storage_root=tmp_path,
    )

    response = service.search("fallback")

    evidence = response.evidence[0]
    assert evidence.expansion == "neighbors"
    assert evidence.context_text == (
        "Previous paragraph.\n\nMatched paragraph.\n\nNext paragraph."
    )
    assert store.fetch_call["node_ids"] == ["previous", "next"]


@pytest.mark.parametrize("limit", [0, 11, True])
def test_retrieval_rejects_invalid_result_limits(tmp_path, limit):
    service = RetrievalService(
        provider=FakeEmbeddingProvider(),
        store=FakeVectorStore(VectorSearchHit("point", 1.0, payload())),
        config=retrieval_config(),
        storage_root=tmp_path,
    )
    with pytest.raises(ValueError, match="retrieval limit"):
        service.search("query", limit=limit)
