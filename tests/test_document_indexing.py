import json
from pathlib import Path

import pytest

from opentalk.documents.config import IndexingConfig, load_indexing_config
from opentalk.documents.index_inspection import inspect_prepared_index
from opentalk.documents.indexing import prepare_index


def indexing_configuration() -> IndexingConfig:
    return IndexingConfig(
        profile="test-profile",
        chunk_max_chars=60,
        chunk_overlap_chars=10,
        parent_max_chars=150,
    )


def element(
    reference: str,
    label: str,
    text: str,
    *,
    page: int = 1,
    parent: str = "#/body",
    visual: str | None = None,
    table_outputs: dict[str, str] | None = None,
    metadata: dict | None = None,
    annotations: list[dict] | None = None,
    content_layer: str = "body",
) -> dict:
    return {
        "element_id": reference,
        "type": "FixtureItem",
        "label": label,
        "level": 1,
        "content_layer": content_layer,
        "parent_id": parent,
        "child_ids": [],
        "text": text,
        "provenance": [
            {
                "page_no": page,
                "bbox": {
                    "left": 1.0,
                    "top": 2.0,
                    "right": 3.0,
                    "bottom": 4.0,
                    "coord_origin": "TOPLEFT",
                },
                "charspan": {"start": 0, "end": len(text)},
            }
        ],
        "visual_path": visual,
        "table_outputs": table_outputs,
        "metadata": metadata,
        "annotations": annotations or [],
    }


def write_parse_fixture(tmp_path: Path, *, status: str = "completed") -> Path:
    parse_path = tmp_path / "documents" / "document-1" / "parses" / "parse-1"
    parse_path.mkdir(parents=True)
    (parse_path / "manifest.json").write_text(
        json.dumps(
            {
                "status": status,
                "document_id": "document-1",
                "parse_version": "parse-1",
                "source_name": "fixture.pdf",
            }
        ),
        encoding="utf-8",
    )
    (parse_path / "document.json").write_text(
        json.dumps(
            {
                "texts": [
                    {"self_ref": "#/texts/2", "label": "section_header", "level": 1},
                    {"self_ref": "#/texts/4", "label": "section_header", "level": 2},
                ]
            }
        ),
        encoding="utf-8",
    )
    (parse_path / "tables").mkdir()
    (parse_path / "tables" / "table-0001.csv").write_text(
        "metric,value\naccuracy,98\nlatency,42\nthroughput,120\n",
        encoding="utf-8",
    )
    (parse_path / "tables" / "table-0001.html").write_text("<table></table>", encoding="utf-8")
    (parse_path / "visuals").mkdir()
    for name in ("picture-0.png", "table-0.png", "formula-0.png"):
        (parse_path / "visuals" / name).write_bytes(b"fixture")
    records = [
        element("#/texts/0", "title", "Research report"),
        element("#/texts/1", "text", "Opening paragraph for the complete report."),
        element("#/texts/2", "section_header", "Results"),
        element(
            "#/texts/3",
            "text",
            "The first detailed result has enough text to require more than one small retrieval chunk. "
            "The following sentence preserves overlap and provenance.",
            page=2,
        ),
        element(
            "#/pictures/0",
            "picture",
            "Architecture diagram",
            page=2,
            visual="visuals/picture-0.png",
            metadata={
                "description": {
                    "text": "A client sends requests to an agent service.<end_of_utterance>",
                    "created_by": "HuggingFaceTB/SmolVLM-256M-Instruct (auto_inline)",
                },
                "classification": {
                    "predictions": [
                        {
                            "class_name": "qr_code",
                            "confidence": 0.0001,
                            "created_by": "DocumentPictureClassifier",
                        }
                    ]
                },
                "keywords": {"values": ["agent service"]},
            },
            annotations=[
                {
                    "kind": "description",
                    "provenance": "HuggingFaceTB/SmolVLM-256M-Instruct (auto_inline)",
                    "text": "A client sends requests to an agent service.<end_of_utterance>",
                },
                {
                    "kind": "classification",
                    "provenance": "DocumentPictureClassifier",
                    "predicted_classes": [{"class_name": "qr_code", "confidence": 0.0001}],
                },
            ],
        ),
        element(
            "#/tables/0",
            "table",
            "Evaluation metrics",
            page=3,
            visual="visuals/table-0.png",
            table_outputs={"csv": "tables/table-0001.csv", "html": "tables/table-0001.html"},
        ),
        element("#/texts/4", "section_header", "Analysis"),
        element("#/texts/5", "formula", "F(x) = x^2", page=4, visual="visuals/formula-0.png"),
        element(
            "#/texts/6",
            "section_header",
            "Heading detected inside a picture",
            parent="#/pictures/0",
        ),
    ]
    (parse_path / "elements.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )
    return parse_path


def read_nodes(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_prepare_index_preserves_hierarchy_chunks_and_non_text_elements(tmp_path):
    parse_path = write_parse_fixture(tmp_path)

    result = prepare_index(parse_path, config=indexing_configuration())

    nodes = read_nodes(result.nodes_path)
    nodes_by_id = {node["node_id"]: node for node in nodes}
    assert len(nodes_by_id) == len(nodes)
    assert nodes[0]["node_type"] == "document"
    assert all(
        node["parent_id"] is None or node["parent_id"] in nodes_by_id for node in nodes
    )
    for node in nodes:
        for child_id in node["child_ids"]:
            assert nodes_by_id[child_id]["parent_id"] == node["node_id"]

    sections = {node["text"]: node for node in nodes if node["node_type"] == "section"}
    assert nodes_by_id[sections["Results"]["parent_id"]]["text"] == "Research report"
    assert nodes_by_id[sections["Analysis"]["parent_id"]]["text"] == "Results"
    assert "Heading detected inside a picture" not in sections

    chunks = [node for node in nodes if node["node_type"] == "chunk"]
    assert len(chunks) >= 3
    assert all(len(node["text"]) <= 60 for node in chunks)
    assert all(node["metadata"]["section_path"] for node in chunks)
    assert any(node["provenance"][0]["page_no"] == 2 for node in chunks)

    picture = next(node for node in nodes if node["node_type"] == "picture")
    assert picture["indexable"] is True
    assert "client sends requests" in picture["embedding_text"]
    assert "agent service" in picture["embedding_text"]
    assert "HuggingFaceTB" not in picture["embedding_text"]
    assert "DocumentPictureClassifier" not in picture["embedding_text"]
    assert "qr_code" not in picture["embedding_text"]
    assert "<end_of_utterance>" not in picture["embedding_text"]
    assert picture["asset_references"][0]["visual"] == "visuals/picture-0.png"

    table = next(node for node in nodes if node["node_type"] == "table")
    table_chunks = [node for node in nodes if node["node_type"] == "table_chunk"]
    assert table["indexable"] is False
    assert table_chunks
    assert all(node["parent_id"] == table["node_id"] for node in table_chunks)
    assert any("accuracy,98" in node["text"] for node in table_chunks)
    assert nodes_by_id[table["parent_id"]]["node_type"] == "context"

    indexable = [node for node in nodes if node["indexable"]]
    for position, node in enumerate(indexable):
        expected_previous = indexable[position - 1]["node_id"] if position else None
        expected_next = indexable[position + 1]["node_id"] if position + 1 < len(indexable) else None
        assert node["previous_id"] == expected_previous
        assert node["next_id"] == expected_next

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["indexable_node_count"] == len(indexable)
    assert manifest["node_counts"]["picture"] == 1
    assert manifest["node_counts"]["table"] == 1
    inspection = inspect_prepared_index(result.output_path)
    assert inspection["valid"] is True
    assert inspection["missing_asset_paths"] == []
    assert inspection["max_chunk_chars"] <= 60


def test_prepare_index_creates_synthetic_section_and_retains_undescribed_picture(tmp_path):
    parse_path = write_parse_fixture(tmp_path)
    records = [
        element("#/texts/0", "text", "Content without a detected heading."),
        element("#/pictures/0", "picture", "", visual="visuals/picture-0.png"),
    ]
    (parse_path / "elements.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    result = prepare_index(parse_path, config=indexing_configuration())
    nodes = read_nodes(result.nodes_path)

    section = next(node for node in nodes if node["node_type"] == "section")
    picture = next(node for node in nodes if node["node_type"] == "picture")
    assert section["metadata"]["synthetic"] is True
    assert picture["indexable"] is False
    assert picture["asset_references"][0]["visual"] == "visuals/picture-0.png"


def test_prepare_index_requires_completed_parse_and_unique_profile(tmp_path):
    failed_parse = write_parse_fixture(tmp_path, status="failed")
    with pytest.raises(ValueError, match="completed parse"):
        prepare_index(failed_parse, config=indexing_configuration())

    completed_parse = write_parse_fixture(tmp_path / "other")
    prepare_index(completed_parse, config=indexing_configuration())
    with pytest.raises(ValueError, match="already exists"):
        prepare_index(completed_parse, config=indexing_configuration())


def test_prepare_index_excludes_only_docling_annotated_furniture(tmp_path):
    parse_path = write_parse_fixture(tmp_path)
    records = [
        element(
            "#/texts/header",
            "page_header",
            "Detected header",
            content_layer="furniture",
        ),
        element(
            "#/texts/header-child",
            "text",
            "Header child",
            parent="#/texts/header",
        ),
        element(
            "#/texts/footer",
            "page_footer",
            "Detected footer",
            content_layer="body",
        ),
        element("#/texts/body-1", "text", "Repeated but unlabelled body text.", page=1),
        element("#/texts/body-2", "text", "Repeated but unlabelled body text.", page=2),
    ]
    records[0].pop("content_layer")
    document = json.loads((parse_path / "document.json").read_text(encoding="utf-8"))
    document["texts"].append(
        {
            "self_ref": "#/texts/header",
            "label": "page_header",
            "content_layer": "furniture",
        }
    )
    (parse_path / "document.json").write_text(json.dumps(document), encoding="utf-8")
    (parse_path / "elements.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    result = prepare_index(parse_path, config=indexing_configuration())
    nodes = read_nodes(result.nodes_path)
    indexed_element_ids = {
        element_id for node in nodes for element_id in node.get("element_ids", [])
    }

    assert "#/texts/header" not in indexed_element_ids
    assert "#/texts/header-child" not in indexed_element_ids
    assert "#/texts/footer" not in indexed_element_ids
    assert "#/texts/body-1" in indexed_element_ids
    assert "#/texts/body-2" in indexed_element_ids
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["excluded_element_counts"] == {
        "page_footer": 1,
        "page_header": 1,
        "text": 1,
    }


def test_load_indexing_config_validates_chunk_limits(tmp_path):
    path = tmp_path / "documents.toml"
    path.write_text(
        """[indexing]
profile = "hierarchical-v2"
chunk_max_chars = 100
chunk_overlap_chars = 20
parent_max_chars = 400
""",
        encoding="utf-8",
    )
    loaded = load_indexing_config(path)
    assert loaded.profile == "hierarchical-v2"
    assert loaded.parent_max_chars == 400

    path.write_text(
        """[indexing]
profile = "hierarchical-v2"
chunk_max_chars = 100
chunk_overlap_chars = 100
parent_max_chars = 400
""",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="chunk_overlap_chars"):
        load_indexing_config(path)
