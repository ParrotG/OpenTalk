"""Load and validate PDF parsing configuration."""

import math
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from opentalk.config import PROJECT_ROOT


@dataclass(frozen=True)
class HeadingHierarchyConfig:
    enabled: bool = True
    use_bookmarks: bool = True
    use_numbering: bool = True
    use_style: bool = True
    use_font_style: bool = True
    style_size_tolerance: float = 0.05
    max_level: int = 6
    bookmark_match_threshold: float = 0.8


@dataclass(frozen=True)
class DocumentAccelerationConfig:
    device: str = "cuda"
    num_threads: int = 8
    parser_threads: int = 8
    ocr_batch_size: int = 8
    layout_batch_size: int = 8
    table_batch_size: int = 4
    picture_description_batch_size: int = 8
    queue_max_size: int = 64
    batch_polling_interval_seconds: float = 0.1
    cuda_use_flash_attention2: bool = False


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
    acceleration: DocumentAccelerationConfig = field(
        default_factory=DocumentAccelerationConfig
    )
    heading_hierarchy: HeadingHierarchyConfig = field(
        default_factory=HeadingHierarchyConfig
    )


@dataclass(frozen=True)
class IndexingConfig:
    profile: str
    chunk_max_chars: int
    chunk_overlap_chars: int
    parent_max_chars: int


@dataclass(frozen=True)
class DocumentAccessConfig:
    max_outline_items: int
    max_page_span: int
    default_content_chars: int
    max_content_chars: int
    default_results: int
    max_results: int
    per_section_limit: int
    max_images: int
    max_image_bytes: int
    fuzzy_score_cutoff: float


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

        acceleration_data = data.get("acceleration", {})
        if not isinstance(acceleration_data, dict):
            raise ValueError("acceleration must be a table.")
        acceleration_defaults = DocumentAccelerationConfig()
        acceleration_values: dict[str, object] = {
            name: acceleration_data.get(name, getattr(acceleration_defaults, name))
            for name in asdict(acceleration_defaults)
        }
        device = acceleration_values["device"]
        if not isinstance(device, str) or re.fullmatch(
            r"(?:auto|cpu|mps|xpu|cuda(?::\d+)?)", device
        ) is None:
            raise ValueError("acceleration.device is invalid.")
        acceleration_integer_names = (
            "num_threads",
            "parser_threads",
            "ocr_batch_size",
            "layout_batch_size",
            "table_batch_size",
            "picture_description_batch_size",
            "queue_max_size",
        )
        if any(
            type(acceleration_values[name]) is not int
            or acceleration_values[name] <= 0
            for name in acceleration_integer_names
        ):
            raise ValueError("Document acceleration integer settings must be positive.")
        largest_batch = max(
            int(acceleration_values[name])
            for name in (
                "ocr_batch_size",
                "layout_batch_size",
                "table_batch_size",
            )
        )
        if acceleration_values["queue_max_size"] < largest_batch:
            raise ValueError("acceleration.queue_max_size must cover every page batch.")
        polling_interval = acceleration_values["batch_polling_interval_seconds"]
        if (
            type(polling_interval) not in (int, float)
            or not math.isfinite(polling_interval)
            or polling_interval <= 0
        ):
            raise ValueError(
                "acceleration.batch_polling_interval_seconds must be positive and finite."
            )
        use_flash_attention = acceleration_values["cuda_use_flash_attention2"]
        if type(use_flash_attention) is not bool:
            raise ValueError("acceleration.cuda_use_flash_attention2 must be a boolean.")
        acceleration_values["batch_polling_interval_seconds"] = float(polling_interval)

        hierarchy_data = data.get("heading_hierarchy", {})
        if not isinstance(hierarchy_data, dict):
            raise ValueError("heading_hierarchy must be a table.")
        hierarchy_defaults = HeadingHierarchyConfig()
        hierarchy_values: dict[str, object] = {
            name: hierarchy_data.get(name, getattr(hierarchy_defaults, name))
            for name in asdict(hierarchy_defaults)
        }
        hierarchy_boolean_names = (
            "enabled",
            "use_bookmarks",
            "use_numbering",
            "use_style",
            "use_font_style",
        )
        if any(type(hierarchy_values[name]) is not bool for name in hierarchy_boolean_names):
            raise ValueError("Heading hierarchy feature switches must be booleans.")
        tolerance = hierarchy_values["style_size_tolerance"]
        if type(tolerance) not in (int, float) or not math.isfinite(tolerance) or tolerance < 0:
            raise ValueError("heading_hierarchy.style_size_tolerance must be non-negative.")
        max_level = hierarchy_values["max_level"]
        if type(max_level) is not int or max_level <= 0:
            raise ValueError("heading_hierarchy.max_level must be a positive integer.")
        threshold = hierarchy_values["bookmark_match_threshold"]
        if (
            type(threshold) not in (int, float)
            or not math.isfinite(threshold)
            or not 0 <= threshold <= 1
        ):
            raise ValueError(
                "heading_hierarchy.bookmark_match_threshold must be between 0 and 1."
            )
        hierarchy_values["style_size_tolerance"] = float(tolerance)
        hierarchy_values["bookmark_match_threshold"] = float(threshold)

        return DocumentConfig(
            storage_root=storage_root,
            max_file_size_bytes=max_file_size_mb * 1024 * 1024,
            max_pages=max_pages,
            document_timeout_seconds=float(timeout),
            images_scale=float(images_scale),
            table_mode=table_mode,
            acceleration=DocumentAccelerationConfig(**acceleration_values),
            heading_hierarchy=HeadingHierarchyConfig(**hierarchy_values),
            **{name: data[name] for name in boolean_names},
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid document configuration at {path}: {error}") from error


def load_indexing_config(path: Path | None = None) -> IndexingConfig:
    """Load deterministic index-preparation settings."""
    path = path or PROJECT_ROOT / "config/documents.toml"
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)["indexing"]

        profile = data["profile"]
        chunk_max_chars = data["chunk_max_chars"]
        chunk_overlap_chars = data["chunk_overlap_chars"]
        parent_max_chars = data["parent_max_chars"]
        if not isinstance(profile, str) or not profile or len(profile) > 100:
            raise ValueError("profile must be a non-empty string of at most 100 characters.")
        allowed_profile_characters = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        )
        if any(character not in allowed_profile_characters for character in profile):
            raise ValueError("profile contains unsupported characters.")
        if type(chunk_max_chars) is not int or chunk_max_chars <= 0:
            raise ValueError("chunk_max_chars must be a positive integer.")
        if (
            type(chunk_overlap_chars) is not int
            or chunk_overlap_chars < 0
            or chunk_overlap_chars >= chunk_max_chars
        ):
            raise ValueError(
                "chunk_overlap_chars must be non-negative and smaller than chunk_max_chars."
            )
        if type(parent_max_chars) is not int or parent_max_chars < chunk_max_chars:
            raise ValueError("parent_max_chars must be an integer at least as large as chunk_max_chars.")
        return IndexingConfig(
            profile=profile,
            chunk_max_chars=chunk_max_chars,
            chunk_overlap_chars=chunk_overlap_chars,
            parent_max_chars=parent_max_chars,
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid indexing configuration at {path}: {error}") from error


def load_document_access_config(path: Path | None = None) -> DocumentAccessConfig:
    """Load bounds for deterministic structure and text access."""
    path = path or PROJECT_ROOT / "config/documents.toml"
    try:
        with path.open("rb") as stream:
            data = tomllib.load(stream)["access"]
        integer_names = (
            "max_outline_items",
            "max_page_span",
            "default_content_chars",
            "max_content_chars",
            "default_results",
            "max_results",
            "per_section_limit",
            "max_images",
            "max_image_bytes",
        )
        if any(type(data[name]) is not int or data[name] <= 0 for name in integer_names):
            raise ValueError("Document access integer limits must be positive.")
        if data["default_content_chars"] > data["max_content_chars"]:
            raise ValueError("default_content_chars must not exceed max_content_chars.")
        if data["default_results"] > data["max_results"]:
            raise ValueError("default_results must not exceed max_results.")
        cutoff = data["fuzzy_score_cutoff"]
        if type(cutoff) not in (int, float) or not math.isfinite(cutoff) or not 0 <= cutoff <= 100:
            raise ValueError("fuzzy_score_cutoff must be between 0 and 100.")
        return DocumentAccessConfig(
            **{name: data[name] for name in integer_names},
            fuzzy_score_cutoff=float(cutoff),
        )
    except (OSError, KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Invalid document access configuration at {path}: {error}") from error
