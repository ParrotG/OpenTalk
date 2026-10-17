"""Prepare inspectable hierarchical nodes from one completed PDF parse."""

import hashlib
import json
import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from opentalk.documents.config import IndexingConfig, load_indexing_config

_HEADING_LABELS = {"title", "section_header"}
_NON_TEXT_LABELS = {"chart", "formula", "picture", "table"}
_DOCLING_FURNITURE_LABELS = {"page-footer", "page-header", "page_footer", "page_header"}
_BREAK_PATTERN = re.compile(r"[\n。！？；.!?;]\s*|[,，、:]\s+|\s+")
_PICTURE_CONTROL_TOKENS = ("<end_of_utterance>",)


@dataclass(frozen=True)
class IndexPreparationResult:
    document_id: str
    parse_version: str
    profile: str
    output_path: Path
    nodes_path: Path
    structure_path: Path
    manifest_path: Path

    def to_dict(self) -> dict[str, str]:
        return {name: str(value) for name, value in asdict(self).items()}


@dataclass
class _Section:
    node: dict[str, Any]
    source_level: int
    content: list[dict[str, Any]]


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _node_id(document_id: str, parse_version: str, profile: str, kind: str, ordinal: int) -> str:
    source = f"{document_id}\0{parse_version}\0{profile}\0{kind}\0{ordinal}"
    return f"node-{hashlib.sha256(source.encode()).hexdigest()[:24]}"


def _load_source(parse_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest_path = parse_path / "manifest.json"
    elements_path = parse_path / "elements.jsonl"
    if not manifest_path.is_file() or not elements_path.is_file():
        raise ValueError("The parse directory must contain manifest.json and elements.jsonl.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "completed":
        raise ValueError("Index preparation requires a completed parse manifest.")
    document_items: dict[str, dict[str, Any]] = {}
    document_path = parse_path / "document.json"
    if document_path.is_file():
        document = json.loads(document_path.read_text(encoding="utf-8"))
        for collection_name in (
            "texts",
            "pictures",
            "tables",
            "groups",
            "key_value_items",
            "form_items",
        ):
            for item in document.get(collection_name, []):
                if isinstance(item, dict) and isinstance(item.get("self_ref"), str):
                    document_items[item["self_ref"]] = item
    elements = []
    for line_number, line in enumerate(elements_path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            element = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"Invalid elements.jsonl record at line {line_number}.") from error
        if not isinstance(element, dict) or not isinstance(element.get("element_id"), str):
            raise ValueError(f"Invalid element record at line {line_number}.")
        document_item = document_items.get(element["element_id"], {})
        if not isinstance(element.get("heading_level"), int):
            heading_level = document_item.get("level")
            element["heading_level"] = heading_level if isinstance(heading_level, int) else None
        if not isinstance(element.get("content_layer"), str):
            content_layer = document_item.get("content_layer")
            element["content_layer"] = content_layer if isinstance(content_layer, str) else None
        elements.append(element)
    return manifest, elements


def _text_values(value: Any) -> Iterable[str]:
    if isinstance(value, str) and value.strip():
        yield value.strip()
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _text_values(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _text_values(nested)


def _clean_picture_text(value: str) -> str:
    for token in _PICTURE_CONTROL_TOKENS:
        value = value.replace(token, "")
    return value.strip()


def _field_text(value: Any, field: str) -> list[str]:
    if isinstance(value, dict):
        selected = value.get(field)
    else:
        selected = value if field == "text" else None
    if isinstance(selected, str) and selected.strip():
        return [_clean_picture_text(selected)]
    if isinstance(selected, list):
        return [
            _clean_picture_text(item)
            for item in selected
            if isinstance(item, str) and item.strip()
        ]
    return []


def _picture_semantic_text(element: dict[str, Any]) -> list[str]:
    """Select semantic picture fields without embedding model or classifier metadata."""
    metadata = element.get("metadata")
    parts: list[str] = []
    if isinstance(metadata, dict):
        parts.extend(_field_text(metadata.get("description"), "text"))
        parts.extend(_field_text(metadata.get("summary"), "text"))
        parts.extend(_field_text(metadata.get("keywords"), "values"))
        parts.extend(_field_text(metadata.get("topics"), "values"))
        parts.extend(_field_text(metadata.get("code"), "text"))
        parts.extend(_field_text(metadata.get("molecule"), "smi"))
        tabular_chart = metadata.get("tabular_chart")
        parts.extend(_field_text(tabular_chart, "title"))
        entities = metadata.get("entities")
        if isinstance(entities, dict) and isinstance(entities.get("mentions"), list):
            for mention in entities["mentions"]:
                parts.extend(_field_text(mention, "text"))
    for annotation in element.get("annotations", []):
        if isinstance(annotation, dict) and annotation.get("kind") == "description":
            parts.extend(_field_text(annotation, "text"))
    return list(dict.fromkeys(part for part in parts if part))


def _table_text(element: dict[str, Any], parse_path: Path) -> str:
    outputs = element.get("table_outputs")
    if not isinstance(outputs, dict) or not isinstance(outputs.get("csv"), str):
        return ""
    csv_path = (parse_path / outputs["csv"]).resolve()
    try:
        csv_path.relative_to(parse_path.resolve())
    except ValueError as error:
        raise ValueError("A table output points outside the parse directory.") from error
    return csv_path.read_text(encoding="utf-8").strip() if csv_path.is_file() else ""


def _element_text(element: dict[str, Any], parse_path: Path) -> str:
    parts: list[str] = []
    primary = element.get("text")
    if isinstance(primary, str) and primary.strip():
        parts.append(primary.strip())
    if element.get("label") == "table":
        table = _table_text(element, parse_path)
        if table:
            parts.append(table)
    if element.get("label") == "picture":
        parts.extend(_picture_semantic_text(element))
    elif element.get("label") in _NON_TEXT_LABELS:
        parts.extend(_text_values(element.get("metadata")))
        parts.extend(_text_values(element.get("annotations")))
    return "\n".join(dict.fromkeys(parts))


def _exclude_docling_furniture(
    elements: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    excluded_ids = {
        element["element_id"]
        for element in elements
        if element.get("content_layer") == "furniture"
        or element.get("label") in _DOCLING_FURNITURE_LABELS
    }
    while True:
        descendants = {
            element["element_id"]
            for element in elements
            if element.get("parent_id") in excluded_ids
        }
        updated = excluded_ids | descendants
        if updated == excluded_ids:
            break
        excluded_ids = updated
    counts: dict[str, int] = {}
    retained = []
    for element in elements:
        if element["element_id"] in excluded_ids:
            label = str(element.get("label") or "unknown")
            counts[label] = counts.get(label, 0) + 1
        else:
            retained.append(element)
    return retained, counts


def _pages(elements: Iterable[dict[str, Any]]) -> list[int]:
    pages = {
        provenance["page_no"]
        for element in elements
        for provenance in element.get("provenance", [])
        if isinstance(provenance, dict) and isinstance(provenance.get("page_no"), int)
    }
    return sorted(pages)


def _asset_references(element: dict[str, Any]) -> dict[str, Any]:
    return {
        "visual": element.get("visual_path"),
        "table": element.get("table_outputs"),
    }


def _provenance(elements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {"element_id": element["element_id"], **value}
        for element in elements
        for value in element.get("provenance", [])
        if isinstance(value, dict)
    ]


def _is_structural_heading(element: dict[str, Any]) -> bool:
    if element.get("label") not in _HEADING_LABELS:
        return False
    parent_id = element.get("parent_id")
    return not (
        isinstance(parent_id, str)
        and (parent_id.startswith("#/pictures/") or parent_id.startswith("#/tables/"))
    )


def _split_text(text: str, maximum: int, overlap: int) -> list[tuple[int, int, str]]:
    if len(text) <= maximum:
        return [(0, len(text), text)] if text else []
    result = []
    start = 0
    while start < len(text):
        hard_end = min(start + maximum, len(text))
        end = hard_end
        if hard_end < len(text):
            candidates = [match.end() for match in _BREAK_PATTERN.finditer(text, start, hard_end)]
            useful = [candidate for candidate in candidates if candidate >= start + maximum // 2]
            if useful:
                end = useful[-1]
        chunk = text[start:end].strip()
        if chunk:
            result.append((start, end, chunk))
        if end >= len(text):
            break
        next_start = max(start + 1, end - overlap)
        while next_start < end and text[next_start].isspace():
            next_start += 1
        start = next_start
    return result


def _new_node(
    *,
    node_id: str,
    kind: str,
    level: int,
    parent_id: str | None,
    text: str,
    embedding_text: str,
    indexable: bool,
    element_ids: list[str],
    page_numbers: list[int],
    provenance: list[dict[str, Any]] | None = None,
    asset_references: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "node_id": node_id,
        "node_type": kind,
        "level": level,
        "parent_id": parent_id,
        "child_ids": [],
        "previous_id": None,
        "next_id": None,
        "indexable": indexable,
        "element_ids": element_ids,
        "page_numbers": page_numbers,
        "provenance": provenance or [],
        "text": text,
        "embedding_text": embedding_text,
        "sparse_text": embedding_text,
        "asset_references": asset_references or [],
        "metadata": metadata or {},
    }


def _section_path(section: _Section, nodes_by_id: dict[str, dict[str, Any]]) -> list[str]:
    values = [section.node["metadata"]["heading"]]
    parent_id = section.node["parent_id"]
    while parent_id and nodes_by_id[parent_id]["node_type"] == "section":
        parent = nodes_by_id[parent_id]
        values.append(parent["metadata"]["heading"])
        parent_id = parent["parent_id"]
    return list(reversed(values))


def _page_range(page_numbers: Iterable[int]) -> dict[str, int | None]:
    pages = sorted(set(page_numbers))
    return {
        "start": pages[0] if pages else None,
        "end": pages[-1] if pages else None,
    }


def _build_structure_catalog(
    nodes: list[dict[str, Any]],
    *,
    document_id: str,
    parse_version: str,
    profile: str,
) -> dict[str, Any]:
    nodes_by_id = {node["node_id"]: node for node in nodes}
    sections = [node for node in nodes if node["node_type"] == "section"]
    section_ids = {node["node_id"] for node in sections}
    direct_content: dict[str, list[str]] = {section_id: [] for section_id in section_ids}

    for section in sections:
        for child_id in section["child_ids"]:
            child = nodes_by_id[child_id]
            if child["node_type"] != "context":
                continue
            direct_content[section["node_id"]].extend(child["child_ids"])

    subtree_pages: dict[str, set[int]] = {}

    def collect_subtree_pages(section_id: str) -> set[int]:
        existing = subtree_pages.get(section_id)
        if existing is not None:
            return existing
        pages = {
            page
            for node_id in direct_content[section_id]
            for page in nodes_by_id[node_id].get("page_numbers", [])
            if isinstance(page, int)
        }
        for child_id in nodes_by_id[section_id]["child_ids"]:
            if child_id in section_ids:
                pages.update(collect_subtree_pages(child_id))
        subtree_pages[section_id] = pages
        return pages

    page_nodes: dict[int, list[str]] = {}
    for section in sections:
        for node_id in direct_content[section["node_id"]]:
            for page in nodes_by_id[node_id].get("page_numbers", []):
                if isinstance(page, int):
                    page_nodes.setdefault(page, []).append(node_id)

    entries = []
    for order, section in enumerate(sections):
        section_id = section["node_id"]
        parent_id = section.get("parent_id")
        child_section_ids = [
            child_id for child_id in section["child_ids"] if child_id in section_ids
        ]
        direct_pages = {
            page
            for node_id in direct_content[section_id]
            for page in nodes_by_id[node_id].get("page_numbers", [])
            if isinstance(page, int)
        }
        entries.append(
            {
                "section_id": section_id,
                "parent_section_id": parent_id if parent_id in section_ids else None,
                "order": order,
                "title": section["text"],
                "level": section["level"],
                "source_level": section.get("metadata", {}).get("source_level"),
                "synthetic": bool(section.get("metadata", {}).get("synthetic")),
                "heading_page_numbers": section.get("page_numbers", []),
                "direct_content_page_range": _page_range(direct_pages),
                "subtree_page_range": _page_range(collect_subtree_pages(section_id)),
                "child_section_ids": child_section_ids,
                "direct_node_ids": direct_content[section_id],
            }
        )

    document = next(node for node in nodes if node["node_type"] == "document")
    return {
        "schema_version": 1,
        "document_id": document_id,
        "parse_version": parse_version,
        "index_profile": profile,
        "document_node_id": document["node_id"],
        "page_numbers": document.get("page_numbers", []),
        "root_section_ids": [
            section["section_id"]
            for section in entries
            if section["parent_section_id"] is None
        ],
        "sections": entries,
        "pages": {str(page): node_ids for page, node_ids in sorted(page_nodes.items())},
    }


def _prepare_sections(
    elements: list[dict[str, Any]],
    parse_path: Path,
    document_node: dict[str, Any],
    identifier: Callable[[str], str],
) -> tuple[list[_Section], list[dict[str, Any]]]:
    sections: list[_Section] = []
    section_stack: list[_Section] = []
    loose_content: list[dict[str, Any]] = []
    nodes = [document_node]
    for element in elements:
        label = element.get("label")
        text = _element_text(element, parse_path)
        if _is_structural_heading(element) and text:
            if label == "title":
                source_level = 0
            else:
                heading_level = element.get("heading_level")
                source_level = heading_level if isinstance(heading_level, int) else 1
            while section_stack and section_stack[-1].source_level >= source_level:
                section_stack.pop()
            parent_id = section_stack[-1].node["node_id"] if section_stack else document_node["node_id"]
            node = _new_node(
                node_id=identifier("section"),
                kind="section",
                level=len(section_stack) + 1,
                parent_id=parent_id,
                text=text,
                embedding_text="",
                indexable=False,
                element_ids=[element["element_id"]],
                page_numbers=_pages([element]),
                provenance=_provenance([element]),
                metadata={"heading": text, "source_level": source_level},
            )
            nodes.append(node)
            section = _Section(node=node, source_level=source_level, content=[])
            sections.append(section)
            section_stack.append(section)
        elif text or label in _NON_TEXT_LABELS:
            target = section_stack[-1].content if section_stack else loose_content
            target.append({**element, "prepared_text": text})
    if loose_content:
        node = _new_node(
            node_id=identifier("section"),
            kind="section",
            level=1,
            parent_id=document_node["node_id"],
            text="Untitled content",
            embedding_text="",
            indexable=False,
            element_ids=[],
            page_numbers=_pages(loose_content),
            provenance=_provenance(loose_content),
            metadata={"heading": "Untitled content", "source_level": None, "synthetic": True},
        )
        nodes.insert(1, node)
        sections.insert(0, _Section(node=node, source_level=0, content=loose_content))
    return sections, nodes


def prepare_index(
    parse_path: Path,
    *,
    config: IndexingConfig | None = None,
    profile: str | None = None,
) -> IndexPreparationResult:
    """Create hierarchical, database-independent indexing artifacts for one parse."""
    config = config or load_indexing_config()
    parse_path = parse_path.resolve()
    source_manifest, elements = _load_source(parse_path)
    elements, excluded_element_counts = _exclude_docling_furniture(elements)
    document_id = source_manifest.get("document_id")
    parse_version = source_manifest.get("parse_version")
    if not isinstance(document_id, str) or not isinstance(parse_version, str):
        raise ValueError("The parse manifest is missing document_id or parse_version.")
    profile = profile or config.profile
    if not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", profile):
        raise ValueError("The indexing profile contains unsupported characters.")
    output_path = parse_path / "indexing" / profile
    if output_path.exists():
        raise ValueError("The indexing profile already exists for this parse version.")
    output_path.mkdir(parents=True)
    manifest_path = output_path / "index_manifest.json"
    nodes_path = output_path / "nodes.jsonl"
    structure_path = output_path / "structure.json"
    started_at = datetime.now(UTC)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "status": "preparing",
        "document_id": document_id,
        "parse_version": parse_version,
        "profile": profile,
        "started_at": started_at.isoformat(),
        "settings": {**asdict(config), "profile": profile},
        "excluded_element_counts": excluded_element_counts,
    }
    _write_json(manifest_path, manifest)

    ordinal = 0

    def identifier(kind: str) -> str:
        nonlocal ordinal
        ordinal += 1
        return _node_id(document_id, parse_version, profile, kind, ordinal)

    try:
        document_node = _new_node(
            node_id=identifier("document"),
            kind="document",
            level=0,
            parent_id=None,
            text=source_manifest.get("source_name", document_id),
            embedding_text="",
            indexable=False,
            element_ids=[],
            page_numbers=_pages(elements),
            metadata={"document_id": document_id, "parse_version": parse_version},
        )
        sections, nodes = _prepare_sections(elements, parse_path, document_node, identifier)
        nodes_by_id = {node["node_id"]: node for node in nodes}
        for node in nodes[1:]:
            nodes_by_id[node["parent_id"]]["child_ids"].append(node["node_id"])

        leaf_nodes: list[dict[str, Any]] = []
        for section in sections:
            heading_path = _section_path(section, nodes_by_id)
            pending_text: list[dict[str, Any]] = []
            section_leaves: list[dict[str, Any]] = []

            def flush_text() -> None:
                if not pending_text:
                    return
                parts = []
                spans = []
                cursor = 0
                for item in pending_text:
                    if parts:
                        cursor += 2
                    start = cursor
                    parts.append(item["prepared_text"])
                    cursor += len(item["prepared_text"])
                    spans.append((start, cursor, item))
                combined = "\n\n".join(parts)
                for start, end, chunk in _split_text(
                    combined, config.chunk_max_chars, config.chunk_overlap_chars
                ):
                    relevant = [item for left, right, item in spans if left < end and right > start]
                    context_prefix = " > ".join(heading_path)
                    section_leaves.append(
                        _new_node(
                            node_id=identifier("chunk"),
                            kind="chunk",
                            level=section.node["level"] + 2,
                            parent_id=None,
                            text=chunk,
                            embedding_text=f"{context_prefix}\n{chunk}",
                            indexable=True,
                            element_ids=[item["element_id"] for item in relevant],
                            page_numbers=_pages(relevant),
                            provenance=_provenance(relevant),
                            metadata={
                                "section_id": section.node["node_id"],
                                "section_path": heading_path,
                            },
                        )
                    )
                pending_text.clear()

            for element in section.content:
                label = element.get("label", "text")
                if label not in _NON_TEXT_LABELS:
                    pending_text.append(element)
                    continue
                flush_text()
                content = element["prepared_text"]
                kind = str(label)
                if kind == "table":
                    table_node = _new_node(
                        node_id=identifier("table"),
                        kind="table",
                        level=section.node["level"] + 2,
                        parent_id=None,
                        text=content,
                        embedding_text="",
                        indexable=False,
                        element_ids=[element["element_id"]],
                        page_numbers=_pages([element]),
                        provenance=_provenance([element]),
                        asset_references=[_asset_references(element)],
                        metadata={
                            "section_id": section.node["node_id"],
                            "section_path": heading_path,
                            "label": label,
                            "context_text": element.get("text") or "[Table]",
                        },
                    )
                    for _, _, chunk in _split_text(
                        content, config.chunk_max_chars, config.chunk_overlap_chars
                    ):
                        child = _new_node(
                            node_id=identifier("table_chunk"),
                            kind="table_chunk",
                            level=table_node["level"] + 1,
                            parent_id=table_node["node_id"],
                            text=chunk,
                            embedding_text=f"{' > '.join(heading_path)}\n{chunk}",
                            indexable=True,
                            element_ids=[element["element_id"]],
                            page_numbers=_pages([element]),
                            provenance=_provenance([element]),
                            asset_references=[_asset_references(element)],
                            metadata={
                                "section_id": section.node["node_id"],
                                "section_path": heading_path,
                                "label": label,
                            },
                        )
                        table_node["child_ids"].append(child["node_id"])
                        leaf_nodes.append(child)
                    section_leaves.append(table_node)
                else:
                    section_leaves.append(
                        _new_node(
                            node_id=identifier(kind),
                            kind=kind,
                            level=section.node["level"] + 2,
                            parent_id=None,
                            text=content,
                            embedding_text=f"{' > '.join(heading_path)}\n{content}",
                            indexable=bool(content),
                            element_ids=[element["element_id"]],
                            page_numbers=_pages([element]),
                            provenance=_provenance([element]),
                            asset_references=[_asset_references(element)],
                            metadata={
                                "section_id": section.node["node_id"],
                                "section_path": heading_path,
                                "label": label,
                            },
                        )
                    )
            flush_text()

            parent_groups: list[list[dict[str, Any]]] = []
            for leaf in section_leaves:
                context_text = leaf["metadata"].get("context_text", leaf["text"])[
                    : config.parent_max_chars
                ]
                size = len(context_text)
                current_size = sum(
                    len(
                        item["metadata"].get("context_text", item["text"])[
                            : config.parent_max_chars
                        ]
                    )
                    for item in parent_groups[-1]
                ) if parent_groups else 0
                separator_size = 2 if parent_groups and parent_groups[-1] else 0
                if not parent_groups or current_size + separator_size + size > config.parent_max_chars:
                    parent_groups.append([])
                parent_groups[-1].append(leaf)
            for group in parent_groups:
                context = "\n\n".join(
                    item["metadata"].get("context_text", item["text"])[
                        : config.parent_max_chars
                    ]
                    for item in group
                )
                context_node = _new_node(
                    node_id=identifier("context"),
                    kind="context",
                    level=section.node["level"] + 1,
                    parent_id=section.node["node_id"],
                    text=context,
                    embedding_text="",
                    indexable=False,
                    element_ids=list(dict.fromkeys(value for item in group for value in item["element_ids"])),
                    page_numbers=sorted({value for item in group for value in item["page_numbers"]}),
                    provenance=[value for item in group for value in item["provenance"]],
                    asset_references=[value for item in group for value in item["asset_references"]],
                    metadata={
                        "section_id": section.node["node_id"],
                        "section_path": heading_path,
                        "content_truncated": any(
                            len(item["metadata"].get("context_text", item["text"]))
                            > config.parent_max_chars
                            for item in group
                        ),
                    },
                )
                section.node["child_ids"].append(context_node["node_id"])
                nodes.append(context_node)
                nodes_by_id[context_node["node_id"]] = context_node
                for leaf in group:
                    leaf["parent_id"] = context_node["node_id"]
                    context_node["child_ids"].append(leaf["node_id"])
                    nodes.append(leaf)
                    nodes_by_id[leaf["node_id"]] = leaf
                    if leaf["node_type"] == "table":
                        for child_id in leaf["child_ids"]:
                            child = next(item for item in leaf_nodes if item["node_id"] == child_id)
                            nodes.append(child)
                            nodes_by_id[child_id] = child

        indexable_nodes = [node for node in nodes if node["indexable"]]
        for previous, current, following in zip(
            [None, *indexable_nodes], indexable_nodes, [*indexable_nodes[1:], None]
        ):
            current["previous_id"] = previous["node_id"] if previous else None
            current["next_id"] = following["node_id"] if following else None

        temporary = nodes_path.with_suffix(".jsonl.tmp")
        temporary.write_text(
            "\n".join(json.dumps(node, ensure_ascii=False, sort_keys=True) for node in nodes) + "\n",
            encoding="utf-8",
        )
        temporary.replace(nodes_path)
        structure = _build_structure_catalog(
            nodes,
            document_id=document_id,
            parse_version=parse_version,
            profile=profile,
        )
        _write_json(structure_path, structure)
        counts: dict[str, int] = {}
        for node in nodes:
            counts[node["node_type"]] = counts.get(node["node_type"], 0) + 1
        manifest.update(
            status="completed",
            completed_at=datetime.now(UTC).isoformat(),
            node_counts=counts,
            indexable_node_count=len(indexable_nodes),
            outputs={"nodes": "nodes.jsonl", "structure": "structure.json"},
        )
        _write_json(manifest_path, manifest)
    except Exception as error:
        manifest.update(
            status="failed",
            completed_at=datetime.now(UTC).isoformat(),
            error={"type": type(error).__name__, "message": str(error)},
        )
        _write_json(manifest_path, manifest)
        raise

    return IndexPreparationResult(
        document_id=document_id,
        parse_version=parse_version,
        profile=profile,
        output_path=output_path,
        nodes_path=nodes_path,
        structure_path=structure_path,
        manifest_path=manifest_path,
    )
