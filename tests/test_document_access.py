import json
from pathlib import Path

import pytest

from opentalk.documents.access import DocumentAccessService
from opentalk.documents.config import (
    DocumentAccessConfig,
    load_document_access_config,
)

DOCUMENT_ID = "a" * 64


def access_config() -> DocumentAccessConfig:
    return DocumentAccessConfig(
        max_outline_items=2,
        max_page_span=2,
        default_content_chars=100,
        max_content_chars=200,
        default_results=3,
        max_results=5,
        per_section_limit=2,
        max_images=2,
        max_image_bytes=1024,
        fuzzy_score_cutoff=70.0,
    )


def write_artifacts(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    parse_path = tmp_path / DOCUMENT_ID / "parses" / "parse-1"
    index_path = parse_path / "indexing" / "hierarchical-v3"
    index_path.mkdir(parents=True)
    visual = parse_path / "visuals" / "diagram.png"
    visual.parent.mkdir()
    visual.write_bytes(b"image")

    identifiers = {
        "document": "document-node",
        "root": "section-root",
        "child": "section-child",
        "appendix": "section-appendix",
        "root_context": "context-root",
        "child_context": "context-child",
        "root_chunk": "chunk-root",
        "picture": "picture-child",
        "child_chunk": "chunk-child",
    }
    nodes = [
        {
            "node_id": identifiers["document"],
            "node_type": "document",
            "parent_id": None,
            "text": "Fixture",
            "page_numbers": [1, 2],
        },
        {
            "node_id": identifiers["root_context"],
            "node_type": "context",
            "parent_id": identifiers["root"],
            "text": "The Alpha identifier explains retrieval architecture in its surrounding context.",
            "page_numbers": [1],
        },
        {
            "node_id": identifiers["root_chunk"],
            "node_type": "chunk",
            "parent_id": identifiers["root_context"],
            "text": "The Alpha   Identifier explains retrieval architecture.",
            "element_ids": ["#/texts/1"],
            "page_numbers": [1],
            "provenance": [{"page_no": 1}],
            "asset_references": [],
            "metadata": {
                "section_id": identifiers["root"],
                "section_path": ["Overview"],
            },
        },
        {
            "node_id": identifiers["child_context"],
            "node_type": "context",
            "parent_id": identifiers["child"],
            "text": "A diagram is followed by the attention explanation.",
            "page_numbers": [2],
        },
        {
            "node_id": identifiers["picture"],
            "node_type": "picture",
            "parent_id": identifiers["child_context"],
            "text": "A blue pipeline diagram showing the agent flow.",
            "element_ids": ["#/pictures/0"],
            "page_numbers": [2],
            "provenance": [{"page_no": 2}],
            "asset_references": [{"visual": "visuals/diagram.png", "table": None}],
            "metadata": {
                "section_id": identifiers["child"],
                "section_path": ["Overview", "Attention"],
            },
        },
        {
            "node_id": identifiers["child_chunk"],
            "node_type": "chunk",
            "parent_id": identifiers["child_context"],
            "text": "Multi-Head Attention transforms the values.",
            "element_ids": ["#/texts/2"],
            "page_numbers": [2],
            "provenance": [{"page_no": 2}],
            "asset_references": [],
            "metadata": {
                "section_id": identifiers["child"],
                "section_path": ["Overview", "Attention"],
            },
        },
    ]
    (index_path / "nodes.jsonl").write_text(
        "\n".join(json.dumps(node) for node in nodes) + "\n",
        encoding="utf-8",
    )
    (index_path / "index_manifest.json").write_text(
        json.dumps({"status": "completed"}),
        encoding="utf-8",
    )
    sections = [
        {
            "section_id": identifiers["root"],
            "parent_section_id": None,
            "order": 0,
            "title": "Overview",
            "level": 1,
            "source_level": 1,
            "synthetic": False,
            "heading_page_numbers": [1],
            "direct_content_page_range": {"start": 1, "end": 1},
            "subtree_page_range": {"start": 1, "end": 2},
            "child_section_ids": [identifiers["child"]],
            "direct_node_ids": [identifiers["root_chunk"]],
        },
        {
            "section_id": identifiers["child"],
            "parent_section_id": identifiers["root"],
            "order": 1,
            "title": "Attention",
            "level": 2,
            "source_level": 2,
            "synthetic": False,
            "heading_page_numbers": [2],
            "direct_content_page_range": {"start": 2, "end": 2},
            "subtree_page_range": {"start": 2, "end": 2},
            "child_section_ids": [],
            "direct_node_ids": [identifiers["picture"], identifiers["child_chunk"]],
        },
        {
            "section_id": identifiers["appendix"],
            "parent_section_id": None,
            "order": 2,
            "title": "Appendix",
            "level": 1,
            "source_level": 1,
            "synthetic": False,
            "heading_page_numbers": [2],
            "direct_content_page_range": {"start": None, "end": None},
            "subtree_page_range": {"start": None, "end": None},
            "child_section_ids": [],
            "direct_node_ids": [],
        },
    ]
    (index_path / "structure.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "document_id": DOCUMENT_ID,
                "parse_version": "parse-1",
                "index_profile": "hierarchical-v3",
                "document_node_id": identifiers["document"],
                "page_numbers": [1, 2],
                "root_section_ids": [identifiers["root"], identifiers["appendix"]],
                "sections": sections,
                "pages": {
                    "1": [identifiers["root_chunk"]],
                    "2": [identifiers["picture"], identifiers["child_chunk"]],
                },
            }
        ),
        encoding="utf-8",
    )
    return visual, identifiers


def service(tmp_path: Path) -> DocumentAccessService:
    return DocumentAccessService(
        storage_root=tmp_path,
        index_profile="hierarchical-v3",
        config=access_config(),
    )


def test_outline_and_section_views_are_bounded_and_cursor_based(tmp_path):
    _, identifiers = write_artifacts(tmp_path)
    access = service(tmp_path)

    outline = access.inspect(DOCUMENT_ID, view="outline", max_items=1).to_dict()
    assert [item["title"] for item in outline["items"]] == ["Overview"]
    assert outline["truncated"] is True
    assert outline["next_cursor"] == "1"
    appendix = access.inspect(
        DOCUMENT_ID,
        view="outline",
        cursor=outline["next_cursor"],
        max_items=1,
    ).to_dict()
    assert [item["title"] for item in appendix["items"]] == ["Appendix"]

    children = access.inspect(
        DOCUMENT_ID,
        view="outline",
        parent_section_id=identifiers["root"],
    ).to_dict()
    assert [item["title"] for item in children["items"]] == ["Attention"]

    first = access.inspect(
        DOCUMENT_ID,
        view="section",
        section_id=identifiers["root"],
        section_scope="subtree",
        max_chars=20,
    ).to_dict()
    assert first["returned_characters"] == 20
    assert first["truncated"] is True
    assert first["next_cursor"] == "0:20"
    continued = access.inspect(
        DOCUMENT_ID,
        view="section",
        section_id=identifiers["root"],
        section_scope="subtree",
        cursor=first["next_cursor"],
        max_chars=100,
    ).to_dict()
    assert continued["items"][0]["text_offset_start"] == 20


def test_page_view_uses_page_map_and_returns_safe_images(tmp_path):
    visual, identifiers = write_artifacts(tmp_path)
    access = service(tmp_path)

    response = access.inspect(DOCUMENT_ID, view="page", page_start=2)
    result = response.to_dict(include_local_paths=True)

    assert [item["node_id"] for item in result["items"]] == [
        identifiers["picture"],
        identifiers["child_chunk"],
    ]
    assert result["local_image_paths"] == [str(visual)]
    assert response.image_paths() == [visual]
    with pytest.raises(ValueError, match="at most 2 pages"):
        access.inspect(DOCUMENT_ID, view="page", page_start=1, page_end=3)


def test_text_find_prefers_normalized_substring_then_falls_back_to_fuzzy(tmp_path):
    write_artifacts(tmp_path)
    access = service(tmp_path)

    normalized = access.find_text(DOCUMENT_ID, "alpha identifier").to_dict()
    assert normalized["applied_match_mode"] == "substring"
    assert normalized["matches"][0]["match_type"] == "normalized_substring"
    assert normalized["matches"][0]["context_text"].startswith("The Alpha identifier")
    assert normalized["matches"][0]["page_numbers"] == [1]

    fuzzy = access.find_text(DOCUMENT_ID, "Multi-Head Atention").to_dict()
    assert fuzzy["applied_match_mode"] == "fuzzy"
    assert fuzzy["matches"][0]["match_type"] == "fuzzy"
    assert fuzzy["matches"][0]["section_path"] == ["Overview", "Attention"]
    assert fuzzy["matches"][0]["score"] >= 70


def test_text_find_supports_structure_and_page_filters(tmp_path):
    _, identifiers = write_artifacts(tmp_path)
    access = service(tmp_path)

    title = access.find_text(DOCUMENT_ID, "Appendx", match_mode="fuzzy").to_dict()
    assert title["matches"][0]["node_type"] == "section"
    assert title["matches"][0]["section_id"] == identifiers["appendix"]

    page = access.find_text(
        DOCUMENT_ID,
        "diagram",
        page_start=2,
        section_id=identifiers["root"],
    ).to_dict()
    assert page["matches"][0]["node_type"] == "picture"
    assert page["matches"][0]["page_numbers"] == [2]

    absent = access.find_text(DOCUMENT_ID, "Alpha", page_start=2).to_dict()
    assert absent["result_count"] == 0


def test_document_access_configuration_validates_bounds(tmp_path):
    path = tmp_path / "documents.toml"
    path.write_text(
        """[access]
max_outline_items = 20
max_page_span = 3
default_content_chars = 1000
max_content_chars = 2000
default_results = 4
max_results = 8
per_section_limit = 2
max_images = 2
max_image_bytes = 1000000
fuzzy_score_cutoff = 76.0
""",
        encoding="utf-8",
    )

    loaded = load_document_access_config(path)

    assert loaded.max_page_span == 3
    assert loaded.fuzzy_score_cutoff == 76.0
