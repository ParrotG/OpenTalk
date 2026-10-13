"""Load and validate PDF parsing configuration."""

import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class DocumentConfig:
    storage_root: Path
    max_file_size_bytes: int
    max_pages: int
    document_timeout_seconds: float
    images_scale: float
    table_mode: str
    do_ocr: bool
    do_picture_description: bool
    do_picture_classification: bool
    do_formula_enrichment: bool
    do_code_enrichment: bool
    do_chart_extraction: bool


def load_document_config(path: Path | None = None) -> DocumentConfig:
    """Load public parser settings without resolving model credentials."""
    path = path or PROJECT_ROOT / "config/documents.toml"
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)["documents"]

        storage_root = Path(data["storage_root"])
        if not storage_root.is_absolute():
            storage_root = PROJECT_ROOT / storage_root

        max_file_size_mb = data["max_file_size_mb"]
        max_pages = data["max_pages"]
        timeout = data["document_timeout_seconds"]
        images_scale = data["images_scale"]
        table_mode = data["table_mode"]
        boolean_names = (
            "do_ocr",
            "do_picture_description",
            "do_picture_classification",
            "do_formula_enrichment",
            "do_code_enrichment",
            "do_chart_extraction",
        )
        if type(max_file_size_mb) is not int or max_file_size_mb <= 0:
            raise ValueError("max_file_size_mb must be a positive integer.")
        if type(max_pages) is not int or max_pages <= 0:
            raise ValueError("max_pages must be a positive integer.")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("document_timeout_seconds must be a positive finite number.")
        if (
            type(images_scale) not in (int, float)
            or not math.isfinite(images_scale)
            or images_scale <= 0
        ):
            raise ValueError("images_scale must be a positive finite number.")
        if table_mode not in {"fast", "accurate"}:
            raise ValueError("table_mode must be fast or accurate.")
        if any(type(data[name]) is not bool for name in boolean_names):
            raise ValueError("Document feature switches must be booleans.")

        return DocumentConfig(
            storage_root=storage_root,
            max_file_size_bytes=max_file_size_mb * 1024 * 1024,
            max_pages=max_pages,
            document_timeout_seconds=float(timeout),
            images_scale=float(images_scale),
            table_mode=table_mode,
            **{name: data[name] for name in boolean_names},
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid document configuration at {path}: {error}") from error
