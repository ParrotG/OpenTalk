import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opentalk.documents.config import DocumentConfig, load_document_config
from opentalk.documents.parser import parse_pdf


class FakeImage:
    def save(self, path, *, format):
        assert format == "PNG"
        Path(path).write_bytes(b"fake-png")


class FakeFrame:
    def to_csv(self, path, *, index):
        assert index is False
        Path(path).write_text("column\nvalue\n", encoding="utf-8")


class FakeMetadata:
    def model_dump(self, **options):
        return {"description": "A described visual."}


class FakeAnnotation:
    def model_dump(self, **options):
        return {"kind": "classification", "label": "diagram"}


class FakeItem:
    def __init__(self, reference, label, *, text=None, parent=None):
        self.self_ref = reference
        self.label = SimpleNamespace(value=label)
        self.text = text
        self.parent = SimpleNamespace(cref=parent) if parent else None
        self.children = []
        self.meta = FakeMetadata() if label == "picture" else None
        self.annotations = [FakeAnnotation()] if label == "picture" else []
        self.prov = [
            SimpleNamespace(
                page_no=1,
                bbox=SimpleNamespace(
                    l=1.0,
                    t=2.0,
                    r=3.0,
                    b=4.0,
                    coord_origin=SimpleNamespace(value="TOPLEFT"),
                ),
                charspan=(0, len(text or "")),
            )
        ]

    def caption_text(self, document):
        return "Figure caption" if self.label.value == "picture" else "Table caption"

    def get_image(self, document):
        return FakeImage() if self.label.value in {"picture", "table", "formula"} else None

    def export_to_html(self, document):
        return "<table><tr><td>value</td></tr></table>"

    def export_to_dataframe(self, document):
        return FakeFrame()


class FakeDocument:
    def __init__(self):
        self.items = [
            (FakeItem("#/texts/0", "section_header", text="Heading"), 1),
            (FakeItem("#/pictures/0", "picture", parent="#/texts/0"), 2),
            (FakeItem("#/tables/0", "table", parent="#/texts/0"), 2),
            (FakeItem("#/texts/1", "formula", text="x^2", parent="#/texts/0"), 2),
        ]

    def iterate_items(self, **options):
        assert options == {"with_groups": True, "traverse_pictures": True}
        yield from self.items

    def save_as_json(self, path, **options):
        Path(path).write_text('{"name":"fixture"}\n', encoding="utf-8")

    def save_as_markdown(self, path, **options):
        Path(path).write_text("# Heading\n", encoding="utf-8")


class FakeBackend:
    version = "test-docling"
    referenced_image_mode = "referenced"

    def __init__(self, document=None, error=None):
        self.document = document or FakeDocument()
        self.error = error
        self.calls = []

    def convert(self, saved_source, config):
        self.calls.append((saved_source, config))
        if self.error is not None:
            raise self.error
        return self.document


def configuration(tmp_path: Path) -> DocumentConfig:
    return DocumentConfig(
        storage_root=tmp_path / "documents",
        max_file_size_bytes=1024,
        max_pages=20,
        document_timeout_seconds=30.0,
        images_scale=1.0,
        table_mode="accurate",
        do_ocr=True,
        do_picture_description=True,
        do_picture_classification=True,
        do_formula_enrichment=True,
        do_code_enrichment=True,
        do_chart_extraction=False,
    )


def test_parse_pdf_saves_global_source_and_versioned_non_text_artifacts(tmp_path):
    source = tmp_path / "paper.pdf"
    source.write_bytes(b"%PDF-1.7\nfixture")
    backend = FakeBackend()

    result = parse_pdf(
        source,
        config=configuration(tmp_path),
        backend=backend,
        parse_version="test-version",
    )

    assert result.source_path.read_bytes() == source.read_bytes()
    assert result.parse_path.parent.name == "parses"
    assert backend.calls == [(result.source_path, configuration(tmp_path))]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "completed"
    assert manifest["parse_version"] == "test-version"
    assert manifest["element_counts"] == {
        "formula": 1,
        "picture": 1,
        "section_header": 1,
        "table": 1,
    }
    records = [json.loads(line) for line in (result.parse_path / "elements.jsonl").read_text().splitlines()]
    assert [record["label"] for record in records] == [
        "section_header",
        "picture",
        "table",
        "formula",
    ]
    assert records[1]["metadata"] == {"description": "A described visual."}
    assert records[1]["visual_path"].endswith(".png")
    assert records[2]["table_outputs"] == {
        "csv": "tables/table-0001.csv",
        "html": "tables/table-0001.html",
    }
    assert records[3]["text"] == "x^2"


def test_parse_failure_keeps_an_inspectable_failed_manifest(tmp_path):
    source = tmp_path / "broken.pdf"
    source.write_bytes(b"%PDF-1.7\nbroken")

    with pytest.raises(RuntimeError, match="conversion failed"):
        parse_pdf(
            source,
            config=configuration(tmp_path),
            backend=FakeBackend(error=RuntimeError("conversion failed")),
            parse_version="failed-version",
        )

    manifests = list((configuration(tmp_path).storage_root).glob("*/parses/failed-version/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"] == {"message": "conversion failed", "type": "RuntimeError"}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"", "empty"),
        (b"not a PDF", "PDF signature"),
        (b"%PDF-" + b"x" * 1024, "file size limit"),
    ],
)
def test_parse_rejects_invalid_input_before_creating_document_storage(tmp_path, content, message):
    source = tmp_path / "paper.pdf"
    source.write_bytes(content)

    with pytest.raises(ValueError, match=message):
        parse_pdf(source, config=configuration(tmp_path), parse_version="unused")

    assert not configuration(tmp_path).storage_root.exists()


def test_document_config_resolves_storage_from_project_root(tmp_path):
    path = tmp_path / "documents.toml"
    path.write_text(
        """[documents]
storage_root = "data/documents"
max_file_size_mb = 2
max_pages = 10
document_timeout_seconds = 15.0
images_scale = 1.5
table_mode = "fast"
do_ocr = true
do_picture_description = true
do_picture_classification = true
do_formula_enrichment = true
do_code_enrichment = false
do_chart_extraction = false
""",
        encoding="utf-8",
    )

    loaded = load_document_config(path)

    assert loaded.storage_root.name == "documents"
    assert loaded.storage_root.parent.name == "data"
    assert loaded.max_file_size_bytes == 2 * 1024 * 1024
    assert loaded.table_mode == "fast"


def test_document_config_rejects_unknown_table_mode(tmp_path):
    path = tmp_path / "documents.toml"
    path.write_text(
        """[documents]
storage_root = "data/documents"
max_file_size_mb = 2
max_pages = 10
document_timeout_seconds = 15.0
images_scale = 1.5
table_mode = "unknown"
do_ocr = true
do_picture_description = true
do_picture_classification = true
do_formula_enrichment = true
do_code_enrichment = false
do_chart_extraction = false
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="table_mode"):
        load_document_config(path)
