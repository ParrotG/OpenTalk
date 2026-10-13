"""Parse PDF files with Docling and persist inspectable, versioned artifacts."""

import hashlib
import json
import re
import shutil
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from opentalk.documents.config import DocumentConfig, load_document_config

_PDF_SIGNATURE = b"%PDF-"
_VISUAL_LABELS = {"formula", "picture", "chart", "table"}


@dataclass(frozen=True)
class DocumentParseResult:
    document_id: str
    parse_version: str
    source_path: Path
    parse_path: Path
    manifest_path: Path

    def to_dict(self) -> dict[str, str]:
        return {name: str(value) for name, value in asdict(self).items()}


class DocumentParserBackend(Protocol):
    version: str
    referenced_image_mode: Any

    def convert(self, source: Path, config: DocumentConfig) -> Any: ...


def _document_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _validate_pdf(path: Path, max_file_size: int) -> None:
    if not path.is_file():
        raise ValueError("The PDF input must be an existing regular file.")
    size = path.stat().st_size
    if size == 0:
        raise ValueError("The PDF input is empty.")
    if size > max_file_size:
        raise ValueError("The PDF input exceeds the configured file size limit.")
    with path.open("rb") as stream:
        if stream.read(len(_PDF_SIGNATURE)) != _PDF_SIGNATURE:
            raise ValueError("The input does not have a valid PDF signature.")


def _new_parse_version(docling_version: str) -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"docling-{docling_version}_{timestamp}_{uuid4().hex[:8]}"


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _reference(value: Any) -> str | None:
    return getattr(value, "cref", None) if value is not None else None


def _provenance(item: Any) -> list[dict[str, Any]]:
    result = []
    for provenance in getattr(item, "prov", []):
        bbox = provenance.bbox
        result.append(
            {
                "page_no": provenance.page_no,
                "bbox": {
                    "left": bbox.l,
                    "top": bbox.t,
                    "right": bbox.r,
                    "bottom": bbox.b,
                    "coord_origin": bbox.coord_origin.value,
                },
                "charspan": {
                    "start": provenance.charspan[0],
                    "end": provenance.charspan[1],
                },
            }
        )
    return result


def _safe_element_name(reference: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", reference.strip("#/")) or "element"


def _item_text(item: Any, document: Any) -> str:
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text
    caption_text = getattr(item, "caption_text", None)
    if callable(caption_text):
        caption = caption_text(document)
        if caption:
            return caption
    return ""


def _save_visual(item: Any, document: Any, directory: Path, label: str, reference: str) -> str | None:
    get_image = getattr(item, "get_image", None)
    if not callable(get_image):
        return None
    image = get_image(document)
    if image is None:
        return None
    path = directory / f"{label}-{_safe_element_name(reference)}.png"
    image.save(path, format="PNG")
    return str(path.relative_to(directory.parent))


def _export_elements(document: Any, parse_path: Path) -> tuple[dict[str, int], list[str]]:
    visual_directory = parse_path / "visuals"
    table_directory = parse_path / "tables"
    visual_directory.mkdir()
    table_directory.mkdir()
    element_path = parse_path / "elements.jsonl"
    counts: dict[str, int] = {}
    warnings: list[str] = []
    records: list[str] = []
    table_number = 0

    for item, level in document.iterate_items(with_groups=True, traverse_pictures=True):
        label_value = getattr(getattr(item, "label", None), "value", None)
        label = label_value or "group"
        counts[label] = counts.get(label, 0) + 1
        reference = item.self_ref
        visual_path = None
        if label in _VISUAL_LABELS:
            try:
                visual_path = _save_visual(item, document, visual_directory, label, reference)
            except Exception as error:
                warnings.append(f"Could not export visual for {reference}: {type(error).__name__}.")

        table_outputs: dict[str, str] | None = None
        if label == "table":
            table_number += 1
            stem = f"table-{table_number:04d}"
            try:
                html_path = table_directory / f"{stem}.html"
                html_path.write_text(item.export_to_html(document), encoding="utf-8")
                csv_path = table_directory / f"{stem}.csv"
                item.export_to_dataframe(document).to_csv(csv_path, index=False)
                table_outputs = {
                    "html": str(html_path.relative_to(parse_path)),
                    "csv": str(csv_path.relative_to(parse_path)),
                }
            except Exception as error:
                warnings.append(f"Could not export table {reference}: {type(error).__name__}.")

        meta = getattr(item, "meta", None)
        annotations = getattr(item, "annotations", None)
        record = {
            "element_id": reference,
            "type": type(item).__name__,
            "label": label,
            "level": level,
            "parent_id": _reference(getattr(item, "parent", None)),
            "child_ids": [_reference(child) for child in getattr(item, "children", [])],
            "text": _item_text(item, document),
            "provenance": _provenance(item),
            "visual_path": visual_path,
            "table_outputs": table_outputs,
            "metadata": meta.model_dump(mode="json", by_alias=True) if meta is not None else None,
            "annotations": [value.model_dump(mode="json", by_alias=True) for value in annotations]
            if annotations is not None
            else [],
        }
        records.append(json.dumps(record, ensure_ascii=False, sort_keys=True))

    temporary = element_path.with_suffix(".jsonl.tmp")
    temporary.write_text("\n".join(records) + ("\n" if records else ""), encoding="utf-8")
    temporary.replace(element_path)
    return counts, warnings


class DoclingParserBackend:
    """Keep the optional Docling dependency behind the parser boundary."""

    def __init__(self) -> None:
        from importlib.metadata import version

        from docling_core.types.doc import ImageRefMode

        self.version = version("docling")
        self.referenced_image_mode = ImageRefMode.REFERENCED

    def convert(self, source: Path, config: DocumentConfig) -> Any:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
        from docling.document_converter import DocumentConverter, PdfFormatOption

        options = PdfPipelineOptions(
            document_timeout=config.document_timeout_seconds,
            do_ocr=config.do_ocr,
            do_picture_description=config.do_picture_description,
            do_picture_classification=config.do_picture_classification,
            do_formula_enrichment=config.do_formula_enrichment,
            do_code_enrichment=config.do_code_enrichment,
            do_chart_extraction=config.do_chart_extraction,
            generate_page_images=True,
            generate_picture_images=True,
            generate_table_images=True,
            images_scale=config.images_scale,
            enable_remote_services=False,
        )
        options.table_structure_options.mode = TableFormerMode(config.table_mode)
        converter = DocumentConverter(
            allowed_formats=[InputFormat.PDF],
            format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
        )
        return converter.convert(
            source,
            max_num_pages=config.max_pages,
            max_file_size=config.max_file_size_bytes,
        ).document


def parse_pdf(
    input_path: Path,
    *,
    config: DocumentConfig | None = None,
    backend: DocumentParserBackend | None = None,
    parse_version: str | None = None,
) -> DocumentParseResult:
    """Parse one PDF into a global, immutable artifact version."""
    config = config or load_document_config()
    source = input_path.resolve()
    _validate_pdf(source, config.max_file_size_bytes)
    document_id = _document_hash(source)
    backend = backend or DoclingParserBackend()
    docling_version = backend.version
    parse_version = parse_version or _new_parse_version(docling_version)
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,200}", parse_version):
        raise ValueError("The parse version contains unsupported characters.")

    document_path = config.storage_root / document_id
    saved_source = document_path / "source.pdf"
    parse_path = document_path / "parses" / parse_version
    if parse_path.exists():
        raise ValueError("The parse version already exists for this document.")
    document_path.mkdir(parents=True, exist_ok=True)
    if not saved_source.exists():
        temporary_source = document_path / "source.pdf.tmp"
        shutil.copyfile(source, temporary_source)
        temporary_source.replace(saved_source)
    parse_path.mkdir(parents=True)

    started_at = datetime.now(UTC)
    manifest_path = parse_path / "manifest.json"
    settings = {
        "max_file_size_bytes": config.max_file_size_bytes,
        "max_pages": config.max_pages,
        "document_timeout_seconds": config.document_timeout_seconds,
        "images_scale": config.images_scale,
        "table_mode": config.table_mode,
        "do_ocr": config.do_ocr,
        "do_picture_description": config.do_picture_description,
        "do_picture_classification": config.do_picture_classification,
        "do_formula_enrichment": config.do_formula_enrichment,
        "do_code_enrichment": config.do_code_enrichment,
        "do_chart_extraction": config.do_chart_extraction,
    }
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "parsing",
        "document_id": document_id,
        "parse_version": parse_version,
        "source_name": source.name,
        "source_size_bytes": source.stat().st_size,
        "parser": "docling",
        "parser_version": docling_version,
        "started_at": started_at.isoformat(),
        "settings": settings,
    }
    _write_json(manifest_path, manifest)

    try:
        document = backend.convert(saved_source, config)
        referenced_assets = parse_path / "assets"
        referenced_assets.mkdir()
        document.save_as_json(
            parse_path / "document.json",
            artifacts_dir=referenced_assets,
            image_mode=backend.referenced_image_mode,
            ensure_ascii=False,
        )
        document.save_as_markdown(
            parse_path / "document.md",
            artifacts_dir=referenced_assets,
            image_mode=backend.referenced_image_mode,
            traverse_pictures=True,
        )
        counts, warnings = _export_elements(document, parse_path)
        manifest.update(
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            element_counts=counts,
            warnings=warnings,
            outputs={
                "docling_json": "document.json",
                "markdown": "document.md",
                "elements": "elements.jsonl",
                "referenced_assets": "assets",
                "visuals": "visuals",
                "tables": "tables",
            },
        )
        _write_json(manifest_path, manifest)
    except BaseException as error:
        manifest.update(
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error={"type": type(error).__name__, "message": str(error)},
        )
        _write_json(manifest_path, manifest)
        raise

    return DocumentParseResult(
        document_id=document_id,
        parse_version=parse_version,
        source_path=saved_source,
        parse_path=parse_path,
        manifest_path=manifest_path,
    )
