"""Validate and summarize prepared indexing artifacts for manual review."""

import json
from collections import Counter
from pathlib import Path
from typing import Any

_BOUNDED_NODE_TYPES = {"chunk", "table_chunk"}
_NON_TEXT_NODE_TYPES = {"chart", "formula", "picture", "table"}


def inspect_prepared_index(output_path: Path, *, sample_size: int = 0) -> dict[str, Any]:
    """Return structural validation errors and review metrics for one indexing profile."""
    if sample_size < 0:
        raise ValueError("sample_size must be non-negative.")
    output_path = output_path.resolve()
    manifest_path = output_path / "index_manifest.json"
    nodes_path = output_path / "nodes.jsonl"
    if not manifest_path.is_file() or not nodes_path.is_file():
        raise ValueError("The indexing directory must contain index_manifest.json and nodes.jsonl.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    nodes = [
        json.loads(line)
        for line in nodes_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    errors: list[str] = []
    identifiers = [node.get("node_id") for node in nodes]
    if len(set(identifiers)) != len(identifiers):
        errors.append("Node IDs are not unique.")
    nodes_by_id = {node.get("node_id"): node for node in nodes}
    for node in nodes:
        node_id = node.get("node_id")
        parent_id = node.get("parent_id")
        if parent_id is not None and parent_id not in nodes_by_id:
            errors.append(f"Node {node_id} references a missing parent.")
        for child_id in node.get("child_ids", []):
            child = nodes_by_id.get(child_id)
            if child is None or child.get("parent_id") != node_id:
                errors.append(f"Node {node_id} has an invalid child reference {child_id}.")
        if node.get("indexable") and not str(node.get("embedding_text", "")).strip():
            errors.append(f"Indexable node {node_id} has empty embedding text.")

    indexable = [node for node in nodes if node.get("indexable")]
    for position, node in enumerate(indexable):
        previous_id = indexable[position - 1]["node_id"] if position else None
        next_id = indexable[position + 1]["node_id"] if position + 1 < len(indexable) else None
        if node.get("previous_id") != previous_id or node.get("next_id") != next_id:
            errors.append(f"Indexable node {node['node_id']} has invalid neighbor references.")

    settings = manifest.get("settings", {})
    chunk_max_chars = settings.get("chunk_max_chars")
    if isinstance(chunk_max_chars, int):
        for node in nodes:
            if node.get("node_type") in _BOUNDED_NODE_TYPES and len(node.get("text", "")) > chunk_max_chars:
                errors.append(f"Node {node['node_id']} exceeds chunk_max_chars.")
    parent_max_chars = settings.get("parent_max_chars")
    if isinstance(parent_max_chars, int):
        for node in nodes:
            if node.get("node_type") == "context" and len(node.get("text", "")) > parent_max_chars:
                errors.append(f"Node {node['node_id']} exceeds parent_max_chars.")

    parse_path = output_path.parent.parent
    missing_assets = []
    for node in nodes:
        for references in node.get("asset_references", []):
            paths = []
            if isinstance(references.get("visual"), str):
                paths.append(references["visual"])
            if isinstance(references.get("table"), dict):
                paths.extend(
                    value for value in references["table"].values() if isinstance(value, str)
                )
            for relative_path in paths:
                asset_path = (parse_path / relative_path).resolve()
                try:
                    asset_path.relative_to(parse_path)
                except ValueError:
                    errors.append(f"Node {node['node_id']} has an asset path outside the parse directory.")
                    continue
                if not asset_path.is_file():
                    missing_assets.append(relative_path)

    counts = Counter(str(node.get("node_type")) for node in nodes)
    expected_counts = manifest.get("node_counts")
    if isinstance(expected_counts, dict) and dict(counts) != expected_counts:
        errors.append("Manifest node counts do not match nodes.jsonl.")
    report = {
        "valid": not errors,
        "errors": errors,
        "document_id": manifest.get("document_id"),
        "parse_version": manifest.get("parse_version"),
        "profile": manifest.get("profile"),
        "node_counts": dict(counts),
        "indexable_node_count": len(indexable),
        "unindexable_non_text_count": sum(
            node.get("node_type") in _NON_TEXT_NODE_TYPES
            and not node.get("indexable")
            and not any(
                nodes_by_id.get(child_id, {}).get("indexable")
                for child_id in node.get("child_ids", [])
            )
            for node in nodes
        ),
        "missing_asset_paths": sorted(set(missing_assets)),
        "page_numbers": sorted(
            {page for node in nodes for page in node.get("page_numbers", []) if isinstance(page, int)}
        ),
        "max_chunk_chars": max(
            (
                len(node.get("text", ""))
                for node in nodes
                if node.get("node_type") in _BOUNDED_NODE_TYPES
            ),
            default=0,
        ),
        "max_context_chars": max(
            (len(node.get("text", "")) for node in nodes if node.get("node_type") == "context"),
            default=0,
        ),
    }
    if sample_size:
        report["indexable_samples"] = [
            {
                "node_id": node["node_id"],
                "node_type": node["node_type"],
                "parent_id": node["parent_id"],
                "page_numbers": node["page_numbers"],
                "section_path": node.get("metadata", {}).get("section_path", []),
                "text_preview": node.get("text", "")[:400],
                "asset_references": node.get("asset_references", []),
            }
            for node in indexable[:sample_size]
        ]
    return report
