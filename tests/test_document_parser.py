import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from opentalk.documents.config import (
    DocumentAccelerationConfig,
    DocumentConfig,
    HeadingHierarchyConfig,
    load_document_config,
)
from opentalk.documents.parser import (
    DoclingParserBackend,
    DocumentConversionResult,
    IncompleteDocumentConversionError,
    parse_pdf,
)


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
    def __init__(
        self,
        reference,
        label,
        *,
        text=None,
        parent=None,
        heading_level=None,
        content_layer="body",
    ):
        self.self_ref = reference
        self.label = SimpleNamespace(value=label)
        self.text = text
        self.level = heading_level
        self.content_layer = SimpleNamespace(value=content_layer)
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
            (FakeItem("#/texts/0", "section_header", text="Heading", heading_level=1), 1),
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

    def __init__(
        self,
        document=None,
        error=None,
        *,
        status="success",
        source_page_count=1,
        processed_page_count=1,
        conversion_errors=(),
    ):
        self.document = document or FakeDocument()
        self.error = error
        self.status = status
        self.source_page_count = source_page_count
        self.processed_page_count = processed_page_count
        self.conversion_errors = conversion_errors
        self.calls = []

    def convert(self, saved_source, config):
        self.calls.append((saved_source, config))
        if self.error is not None:
            raise self.error
        return DocumentConversionResult(
            document=self.document,
            status=self.status,
            source_page_count=self.source_page_count,
            processed_page_count=self.processed_page_count,
            errors=self.conversion_errors,
        )


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


def test_docling_backend_enables_heading_hierarchy_with_parsed_pages(monkeypatch, tmp_path):
    captured = {}

    class PipelineOptions:
        def __init__(self, **values):
            captured["pipeline"] = values
            self.table_structure_options = SimpleNamespace(mode=None)
            self.picture_description_options = SimpleNamespace(batch_size=None)
            self.heading_hierarchy_options = None

    class AcceleratorOptions:
        def __init__(self, **values):
            captured["accelerator"] = values

    class BackendOptions:
        def __init__(self, **values):
            captured["backend"] = values

    class HierarchyOptions:
        def __init__(self, **values):
            captured["hierarchy"] = values

    class FormatOption:
        def __init__(self, *, pipeline_options, backend_options):
            captured["options"] = pipeline_options
            captured["backend_options"] = backend_options

    class Converter:
        def __init__(self, **values):
            captured["converter"] = values

        def convert(self, source, **values):
            captured["convert"] = {"source": source, **values}
            error = SimpleNamespace(model_dump=lambda **options: {"error_message": "warning"})
            return SimpleNamespace(
                document="converted-document",
                status=SimpleNamespace(value="success"),
                input=SimpleNamespace(page_count=20),
                pages=[object()] * 20,
                errors=[error],
            )

    modules = {
        "docling": ModuleType("docling"),
        "docling.datamodel": ModuleType("docling.datamodel"),
        "docling.datamodel.accelerator_options": ModuleType(
            "docling.datamodel.accelerator_options"
        ),
        "docling.datamodel.backend_options": ModuleType(
            "docling.datamodel.backend_options"
        ),
        "docling.datamodel.base_models": ModuleType("docling.datamodel.base_models"),
        "docling.datamodel.pipeline_options": ModuleType(
            "docling.datamodel.pipeline_options"
        ),
        "docling.document_converter": ModuleType("docling.document_converter"),
    }
    modules["docling.datamodel.accelerator_options"].AcceleratorOptions = (
        AcceleratorOptions
    )
    modules["docling.datamodel.backend_options"].ThreadedDoclingParseBackendOptions = (
        BackendOptions
    )
    modules["docling.datamodel.base_models"].InputFormat = SimpleNamespace(PDF="pdf")
    pipeline_module = modules["docling.datamodel.pipeline_options"]
    pipeline_module.HeadingHierarchyOptions = HierarchyOptions
    pipeline_module.PdfPipelineOptions = PipelineOptions
    pipeline_module.TableFormerMode = lambda value: value
    converter_module = modules["docling.document_converter"]
    converter_module.DocumentConverter = Converter
    converter_module.PdfFormatOption = FormatOption
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    source = tmp_path / "source.pdf"
    result = DoclingParserBackend.__new__(DoclingParserBackend).convert(
        source,
        configuration(tmp_path),
    )

    assert result == DocumentConversionResult(
        document="converted-document",
        status="success",
        source_page_count=20,
        processed_page_count=20,
        errors=({"error_message": "warning"},),
    )
    assert captured["pipeline"]["generate_parsed_pages"] is True
    assert captured["accelerator"] == {
        "device": "cuda",
        "num_threads": 8,
        "cuda_use_flash_attention2": False,
    }
    assert {
        name: captured["pipeline"][name]
        for name in (
            "ocr_batch_size",
            "layout_batch_size",
            "table_batch_size",
            "queue_max_size",
            "batch_polling_interval_seconds",
        )
    } == {
        "ocr_batch_size": 4,
        "layout_batch_size": 4,
        "table_batch_size": 2,
        "queue_max_size": 8,
        "batch_polling_interval_seconds": 0.1,
    }
    assert captured["options"].picture_description_options.batch_size == 4
    assert captured["backend"] == {
        "parser_threads": 4,
        "render_pages": True,
        "render_scale": 1.0,
    }
    assert captured["hierarchy"] == {
        "enabled": True,
        "use_bookmarks": True,
        "use_numbering": True,
        "use_style": True,
        "use_font_style": True,
        "style_size_tolerance": 0.05,
        "max_level": 6,
        "bookmark_match_threshold": 0.8,
    }
    assert captured["options"].heading_hierarchy_options.__class__ is HierarchyOptions
    assert captured["convert"]["max_num_pages"] == 20


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
    assert manifest["settings"]["heading_hierarchy"]["enabled"] is True
    assert manifest["settings"]["generate_parsed_pages"] is True
    assert manifest["settings"]["acceleration"] == {
        "device": "cuda",
        "num_threads": 8,
        "parser_threads": 4,
        "ocr_batch_size": 4,
        "layout_batch_size": 4,
        "table_batch_size": 2,
        "picture_description_batch_size": 4,
        "queue_max_size": 8,
        "batch_polling_interval_seconds": 0.1,
        "cuda_use_flash_attention2": False,
    }
    assert manifest["conversion"] == {
        "status": "success",
        "source_page_count": 1,
        "processed_page_count": 1,
        "error_count": 0,
        "errors": [],
        "errors_truncated": False,
    }
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
    assert records[0]["heading_level"] == 1
    assert records[0]["content_layer"] == "body"
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
    ("status", "source_pages", "processed_pages"),
    [
        ("partial_success", 629, 253),
        ("success", 629, 628),
    ],
)
def test_incomplete_conversion_is_not_published_as_completed(
    tmp_path,
    status,
    source_pages,
    processed_pages,
):
    source = tmp_path / "large.pdf"
    source.write_bytes(b"%PDF-1.7\nlarge")
    conversion_errors = (
        {
            "component_type": "pipeline",
            "module_name": "StandardPdfPipeline",
            "error_message": "Pipeline stage timeout.",
            "category": "timeout",
            "page_no": None,
        },
    )

    with pytest.raises(IncompleteDocumentConversionError, match=f"{processed_pages} of {source_pages}"):
        parse_pdf(
            source,
            config=configuration(tmp_path),
            backend=FakeBackend(
                status=status,
                source_page_count=source_pages,
                processed_page_count=processed_pages,
                conversion_errors=conversion_errors,
            ),
            parse_version=f"incomplete-{status}",
        )

    manifest_path = next(
        configuration(tmp_path).storage_root.glob(
            f"*/parses/incomplete-{status}/manifest.json"
        )
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["error"]["type"] == "IncompleteDocumentConversionError"
    assert manifest["error"]["conversion"] == {
        "status": status,
        "source_page_count": source_pages,
        "processed_page_count": processed_pages,
        "error_count": 1,
        "errors": list(conversion_errors),
        "errors_truncated": False,
    }
    assert not (manifest_path.parent / "document.json").exists()


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

[documents.heading_hierarchy]
enabled = true
use_bookmarks = true
use_numbering = true
use_style = false
use_font_style = true
style_size_tolerance = 0.04
max_level = 5
bookmark_match_threshold = 0.9
""",
        encoding="utf-8",
    )

    loaded = load_document_config(path)

    assert loaded.storage_root.name == "documents"
    assert loaded.storage_root.parent.name == "data"
    assert loaded.max_file_size_bytes == 2 * 1024 * 1024
    assert loaded.table_mode == "fast"
    assert loaded.heading_hierarchy == HeadingHierarchyConfig(
        enabled=True,
        use_bookmarks=True,
        use_numbering=True,
        use_style=False,
        use_font_style=True,
        style_size_tolerance=0.04,
        max_level=5,
        bookmark_match_threshold=0.9,
    )


def test_default_document_capacity_covers_large_books():
    loaded = load_document_config()

    assert loaded.max_pages == 1000
    assert loaded.max_file_size_bytes == 500 * 1024 * 1024
    assert loaded.document_timeout_seconds == 7200.0
    assert loaded.images_scale == 1.0
    assert loaded.acceleration == DocumentAccelerationConfig()


def test_document_acceleration_configuration_is_loaded(tmp_path):
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

[documents.acceleration]
device = "cuda:1"
num_threads = 6
parser_threads = 5
ocr_batch_size = 7
layout_batch_size = 6
table_batch_size = 3
picture_description_batch_size = 4
queue_max_size = 20
batch_polling_interval_seconds = 0.25
cuda_use_flash_attention2 = true
""",
        encoding="utf-8",
    )

    loaded = load_document_config(path)

    assert loaded.acceleration == DocumentAccelerationConfig(
        device="cuda:1",
        num_threads=6,
        parser_threads=5,
        ocr_batch_size=7,
        layout_batch_size=6,
        table_batch_size=3,
        picture_description_batch_size=4,
        queue_max_size=20,
        batch_polling_interval_seconds=0.25,
        cuda_use_flash_attention2=True,
    )


def test_document_device_environment_overrides_file(monkeypatch, tmp_path):
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

[documents.acceleration]
device = "cuda"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENTALK_DOCUMENT_DEVICE", "cpu")

    loaded = load_document_config(path)

    assert loaded.acceleration.device == "cpu"


@pytest.mark.parametrize(
    ("acceleration", "message"),
    (
        ('device = "cuda:bad"', "acceleration.device"),
        ("ocr_batch_size = 0", "integer settings"),
        (
            "ocr_batch_size = 9\nqueue_max_size = 8",
            "queue_max_size",
        ),
        (
            'batch_polling_interval_seconds = "fast"',
            "batch_polling_interval_seconds",
        ),
    ),
)
def test_document_config_rejects_invalid_acceleration(
    tmp_path, acceleration, message
):
    path = tmp_path / "documents.toml"
    path.write_text(
        f"""[documents]
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

[documents.acceleration]
{acceleration}
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_document_config(path)


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
