"""Deterministic structure browsing and text lookup over prepared document files."""

import json
import re
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

from rapidfuzz import fuzz

from opentalk.documents.config import (
    DocumentAccessConfig,
    load_document_access_config,
    load_document_config,
    load_indexing_config,
)

_SAFE_SEGMENT = re.compile(r"[a-zA-Z0-9._-]{1,200}")
_MAX_CACHED_DOCUMENTS = 4


@dataclass(frozen=True)
class DocumentAccessResponse:
    value: dict[str, Any]
    _image_paths: tuple[Path, ...] = field(default=(), repr=False)

    def to_dict(self, *, include_local_paths: bool = False) -> dict[str, Any]:
        result = dict(self.value)
        if include_local_paths:
            result["local_image_paths"] = [str(path) for path in self._image_paths]
        return result

    def image_paths(self) -> list[Path]:
        return list(self._image_paths)


class DocumentAccessor(Protocol):
    config: DocumentAccessConfig

    def inspect(self, document_id: str, **options: Any) -> DocumentAccessResponse: ...

    def find_text(
        self,
        document_id: str,
        query: str,
        **options: Any,
    ) -> DocumentAccessResponse: ...


@dataclass(frozen=True)
class _Artifacts:
    document_id: str
    parse_version: str
    index_profile: str
    parse_path: Path
    index_path: Path
    structure: dict[str, Any]
    nodes_by_id: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class _TextCandidate:
    candidate_id: str
    text: str
    node_type: str
    section_id: str
    section_path: list[str]
    page_numbers: list[int]
    order: int
    node: dict[str, Any] | None


@dataclass(frozen=True)
class _TextMatch:
    candidate: _TextCandidate
    match_type: Literal["substring", "normalized_substring", "fuzzy"]
    priority: int
    score: float
    start: int
    end: int


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"Could not read document artifact {path.name}.") from error
    if not isinstance(value, dict):
        raise ValueError(f"Document artifact {path.name} must contain a JSON object.")
    return value


def _safe_segment(name: str, value: str) -> str:
    if not isinstance(value, str) or _SAFE_SEGMENT.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters.")
    return value


def _cursor(value: str, *, content: bool) -> tuple[int, int]:
    if not value:
        return 0, 0
    parts = value.split(":", 1) if content else [value]
    if any(not part.isdigit() for part in parts):
        raise ValueError("The document cursor is invalid.")
    position = int(parts[0])
    offset = int(parts[1]) if len(parts) == 2 else 0
    return position, offset


def _normalize_with_offsets(value: str) -> tuple[str, list[int]]:
    characters: list[str] = []
    offsets: list[int] = []
    for source_index, source_character in enumerate(value):
        normalized = unicodedata.normalize("NFKC", source_character).casefold()
        for character in normalized:
            if character.isspace():
                if characters and characters[-1] != " ":
                    characters.append(" ")
                    offsets.append(source_index)
            else:
                characters.append(character)
                offsets.append(source_index)
    while characters and characters[-1] == " ":
        characters.pop()
        offsets.pop()
    return "".join(characters), offsets


def _original_span(offsets: list[int], start: int, end: int, text_length: int) -> tuple[int, int]:
    if not offsets or start >= len(offsets):
        return 0, min(text_length, 1)
    left = offsets[max(0, start)]
    right_index = min(max(start, end - 1), len(offsets) - 1)
    return left, min(text_length, offsets[right_index] + 1)


class DocumentAccessService:
    """Read immutable prepared artifacts with bounded, cursor-based responses."""

    def __init__(
        self,
        *,
        storage_root: Path | None = None,
        index_profile: str | None = None,
        config: DocumentAccessConfig | None = None,
    ) -> None:
        self.storage_root = (
            storage_root or load_document_config().storage_root
        ).resolve()
        self.index_profile = index_profile or load_indexing_config().profile
        _safe_segment("index_profile", self.index_profile)
        self.config = config or load_document_access_config()
        self._cache: OrderedDict[tuple[str, str, str], _Artifacts] = OrderedDict()

    def inspect(
        self,
        document_id: str,
        *,
        view: Literal["outline", "section", "page"],
        section_id: str | None = None,
        parent_section_id: str | None = None,
        section_scope: Literal["direct", "subtree"] = "direct",
        page_start: int | None = None,
        page_end: int | None = None,
        cursor: str = "",
        max_items: int | None = None,
        max_chars: int | None = None,
        parse_version: str | None = None,
        index_profile: str | None = None,
    ) -> DocumentAccessResponse:
        artifacts = self._artifacts(document_id, parse_version, index_profile)
        if view == "outline":
            return self._outline(
                artifacts,
                parent_section_id=parent_section_id,
                cursor=cursor,
                max_items=max_items,
            )
        if view == "section":
            if not section_id:
                raise ValueError("section_id is required for a section view.")
            node_ids = self._section_node_ids(artifacts, section_id, section_scope)
            heading = self._sections_by_id(artifacts).get(section_id)
            response = self._content_response(
                artifacts,
                node_ids,
                view="section",
                cursor=cursor,
                max_chars=max_chars,
            )
            response.value["section"] = self._compact_section(heading) if heading else None
            response.value["section_scope"] = section_scope
            return response
        if view == "page":
            start, end = self._page_range(page_start, page_end)
            node_ids: list[str] = []
            seen: set[str] = set()
            for page in range(start, end + 1):
                for node_id in artifacts.structure.get("pages", {}).get(str(page), []):
                    if node_id not in seen:
                        seen.add(node_id)
                        node_ids.append(node_id)
            response = self._content_response(
                artifacts,
                node_ids,
                view="page",
                cursor=cursor,
                max_chars=max_chars,
            )
            response.value["page_start"] = start
            response.value["page_end"] = end
            return response
        raise ValueError("view must be outline, section, or page.")

    def find_text(
        self,
        document_id: str,
        query: str,
        *,
        match_mode: Literal["auto", "substring", "fuzzy"] = "auto",
        section_id: str | None = None,
        page_start: int | None = None,
        page_end: int | None = None,
        limit: int | None = None,
        parse_version: str | None = None,
        index_profile: str | None = None,
    ) -> DocumentAccessResponse:
        normalized_query, _ = _normalize_with_offsets(query)
        if not normalized_query:
            raise ValueError("The text query must not be empty.")
        if len(normalized_query) > 300:
            raise ValueError("The text query exceeds the 300-character limit.")
        if match_mode not in {"auto", "substring", "fuzzy"}:
            raise ValueError("match_mode must be auto, substring, or fuzzy.")
        result_limit = self.config.default_results if limit is None else limit
        if type(result_limit) is not int or not 1 <= result_limit <= self.config.max_results:
            raise ValueError(
                f"The text result limit must be between 1 and {self.config.max_results}."
            )
        artifacts = self._artifacts(document_id, parse_version, index_profile)
        page_range = None
        if page_start is not None or page_end is not None:
            page_range = self._page_range(page_start, page_end)
        candidates = self._text_candidates(
            artifacts,
            section_id=section_id,
            page_range=page_range,
        )
        exact = [
            match
            for candidate in candidates
            if (match := self._match_candidate(candidate, query, normalized_query, fuzzy=False))
            is not None
        ]
        use_fuzzy = match_mode == "fuzzy" or (match_mode == "auto" and not exact)
        if match_mode == "substring":
            matches = exact
        elif use_fuzzy:
            if len(normalized_query.replace(" ", "")) < 3:
                if match_mode == "fuzzy":
                    raise ValueError("Fuzzy matching requires at least three characters.")
                matches = exact
            else:
                matches = [
                    match
                    for candidate in candidates
                    if (
                        match := self._match_candidate(
                            candidate,
                            query,
                            normalized_query,
                            fuzzy=True,
                        )
                    )
                    is not None
                ]
        else:
            matches = exact
        matches.sort(key=self._match_sort_key)
        deduplicated = self._deduplicate_matches(matches)
        total_matches = len(deduplicated)
        selected: list[_TextMatch] = []
        per_section: dict[str, int] = {}
        for match in deduplicated:
            count = per_section.get(match.candidate.section_id, 0)
            if section_id is None and count >= self.config.per_section_limit:
                continue
            per_section[match.candidate.section_id] = count + 1
            selected.append(match)
            if len(selected) >= result_limit:
                break
        return self._match_response(
            artifacts,
            query=query,
            requested_mode=match_mode,
            applied_mode="fuzzy" if use_fuzzy else "substring",
            matches=selected,
            total_matches=total_matches,
        )

    def _artifacts(
        self,
        document_id: str,
        parse_version: str | None,
        index_profile: str | None,
    ) -> _Artifacts:
        document_id = _safe_segment("document_id", document_id)
        profile = _safe_segment("index_profile", index_profile or self.index_profile)
        document_path = (self.storage_root / document_id).resolve()
        if not document_path.is_relative_to(self.storage_root) or not document_path.is_dir():
            raise ValueError("The document does not exist.")
        if parse_version is None:
            candidates = []
            for path in (document_path / "parses").glob("*"):
                index_path = path / "indexing" / profile
                try:
                    manifest = (
                        _load_json(index_path / "index_manifest.json")
                        if index_path.is_dir()
                        else None
                    )
                except ValueError:
                    continue
                if (
                    manifest
                    and manifest.get("status") == "completed"
                    and (index_path / "structure.json").is_file()
                ):
                    candidates.append((index_path.stat().st_mtime, path.name))
            if not candidates:
                raise ValueError("The document has no completed structure index.")
            parse_version = max(candidates)[1]
        parse_version = _safe_segment("parse_version", parse_version)
        key = (document_id, parse_version, profile)
        cached = self._cache.get(key)
        if cached is not None and cached.index_path.is_dir():
            self._cache.move_to_end(key)
            return cached
        parse_path = document_path / "parses" / parse_version
        index_path = parse_path / "indexing" / profile
        manifest = _load_json(index_path / "index_manifest.json")
        if manifest.get("status") != "completed":
            raise ValueError("The requested structure index is not completed.")
        structure = _load_json(index_path / "structure.json")
        nodes: dict[str, dict[str, Any]] = {}
        try:
            lines = (index_path / "nodes.jsonl").read_text(encoding="utf-8").splitlines()
        except OSError as error:
            raise ValueError("Could not read nodes.jsonl.") from error
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                node = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid nodes.jsonl record at line {line_number}.") from error
            node_id = node.get("node_id") if isinstance(node, dict) else None
            if not isinstance(node_id, str) or node_id in nodes:
                raise ValueError(f"Invalid nodes.jsonl record at line {line_number}.")
            nodes[node_id] = node
        artifacts = _Artifacts(
            document_id=document_id,
            parse_version=parse_version,
            index_profile=profile,
            parse_path=parse_path,
            index_path=index_path,
            structure=structure,
            nodes_by_id=nodes,
        )
        self._cache[key] = artifacts
        self._cache.move_to_end(key)
        while len(self._cache) > _MAX_CACHED_DOCUMENTS:
            self._cache.popitem(last=False)
        return artifacts

    def _outline(
        self,
        artifacts: _Artifacts,
        *,
        parent_section_id: str | None,
        cursor: str,
        max_items: int | None,
    ) -> DocumentAccessResponse:
        if parent_section_id:
            _safe_segment("parent_section_id", parent_section_id)
            if parent_section_id not in self._sections_by_id(artifacts):
                raise ValueError("The parent section does not exist.")
        requested = self.config.max_outline_items if max_items is None else max_items
        if type(requested) is not int or not 1 <= requested <= self.config.max_outline_items:
            raise ValueError(
                f"max_items must be between 1 and {self.config.max_outline_items}."
            )
        start, _ = _cursor(cursor, content=False)
        sections = [
            section
            for section in artifacts.structure.get("sections", [])
            if section.get("parent_section_id") == parent_section_id
        ]
        if start > len(sections):
            raise ValueError("The document cursor is out of range.")
        selected = sections[start : start + requested]
        end = start + len(selected)
        return DocumentAccessResponse(
            self._base_response(artifacts)
            | {
                "view": "outline",
                "parent_section_id": parent_section_id,
                "items": [self._compact_section(section) for section in selected],
                "returned_items": len(selected),
                "total_items": len(sections),
                "truncated": end < len(sections),
                "next_cursor": str(end) if end < len(sections) else None,
            }
        )

    def _content_response(
        self,
        artifacts: _Artifacts,
        node_ids: list[str],
        *,
        view: str,
        cursor: str,
        max_chars: int | None,
    ) -> DocumentAccessResponse:
        character_limit = self.config.default_content_chars if max_chars is None else max_chars
        if (
            type(character_limit) is not int
            or not 1 <= character_limit <= self.config.max_content_chars
        ):
            raise ValueError(
                f"max_chars must be between 1 and {self.config.max_content_chars}."
            )
        position, offset = _cursor(cursor, content=True)
        if position > len(node_ids):
            raise ValueError("The document cursor is out of range.")
        items: list[dict[str, Any]] = []
        image_paths: list[Path] = []
        used = 0
        while position < len(node_ids) and len(items) < self.config.max_results:
            node = artifacts.nodes_by_id.get(node_ids[position])
            if node is None:
                raise ValueError("The structure catalog references a missing node.")
            text = str(node.get("text") or "")
            if offset > len(text):
                raise ValueError("The document cursor is out of range.")
            available = character_limit - used
            if text and available <= 0:
                break
            end = min(len(text), offset + available) if text else 0
            item = self._content_item(node, text[offset:end], offset, end)
            items.append(item)
            image_paths.extend(self._resolve_images(artifacts, node))
            used += end - offset
            if end < len(text):
                offset = end
                break
            position += 1
            offset = 0
        has_more = position < len(node_ids)
        next_cursor = f"{position}:{offset}" if has_more else None
        return DocumentAccessResponse(
            self._base_response(artifacts)
            | {
                "view": view,
                "items": items,
                "returned_items": len(items),
                "returned_characters": used,
                "total_nodes": len(node_ids),
                "truncated": has_more,
                "next_cursor": next_cursor,
            },
            tuple(dict.fromkeys(image_paths)),
        )

    def _text_candidates(
        self,
        artifacts: _Artifacts,
        *,
        section_id: str | None,
        page_range: tuple[int, int] | None,
    ) -> list[_TextCandidate]:
        sections = artifacts.structure.get("sections", [])
        section_map = self._sections_by_id(artifacts)
        allowed_sections: set[str] | None = None
        if section_id:
            if section_id not in section_map:
                raise ValueError("The section does not exist.")
            allowed_sections = {section_id}
            pending = [section_id]
            while pending:
                current = pending.pop()
                children = section_map[current].get("child_section_ids", [])
                allowed_sections.update(children)
                pending.extend(children)

        candidates: list[_TextCandidate] = []
        seen_nodes: set[str] = set()
        for section in sections:
            current_section_id = section["section_id"]
            if allowed_sections is not None and current_section_id not in allowed_sections:
                continue
            heading_pages = [
                page for page in section.get("heading_page_numbers", []) if isinstance(page, int)
            ]
            if self._pages_match(heading_pages, page_range):
                candidates.append(
                    _TextCandidate(
                        candidate_id=current_section_id,
                        text=str(section.get("title") or ""),
                        node_type="section",
                        section_id=current_section_id,
                        section_path=self._section_path(section_map, current_section_id),
                        page_numbers=heading_pages,
                        order=int(section.get("order", len(candidates))),
                        node=None,
                    )
                )
            for node_id in section.get("direct_node_ids", []):
                if node_id in seen_nodes:
                    continue
                seen_nodes.add(node_id)
                node = artifacts.nodes_by_id.get(node_id)
                if node is None:
                    raise ValueError("The structure catalog references a missing node.")
                pages = [page for page in node.get("page_numbers", []) if isinstance(page, int)]
                if not self._pages_match(pages, page_range):
                    continue
                text = str(node.get("text") or "")
                if not text:
                    continue
                metadata = node.get("metadata", {})
                candidates.append(
                    _TextCandidate(
                        candidate_id=node_id,
                        text=text,
                        node_type=str(node.get("node_type") or "content"),
                        section_id=current_section_id,
                        section_path=[str(value) for value in metadata.get("section_path", [])],
                        page_numbers=pages,
                        order=len(candidates),
                        node=node,
                    )
                )
        return candidates

    def _match_candidate(
        self,
        candidate: _TextCandidate,
        raw_query: str,
        normalized_query: str,
        *,
        fuzzy: bool,
    ) -> _TextMatch | None:
        raw_start = candidate.text.find(raw_query)
        if raw_start >= 0:
            return _TextMatch(
                candidate,
                "substring",
                3,
                100.0,
                raw_start,
                raw_start + len(raw_query),
            )
        normalized_text, offsets = _normalize_with_offsets(candidate.text)
        normalized_start = normalized_text.find(normalized_query)
        if normalized_start >= 0:
            start, end = _original_span(
                offsets,
                normalized_start,
                normalized_start + len(normalized_query),
                len(candidate.text),
            )
            return _TextMatch(candidate, "normalized_substring", 2, 100.0, start, end)
        if not fuzzy or not normalized_text:
            return None
        alignment = fuzz.partial_ratio_alignment(
            normalized_query,
            normalized_text,
            score_cutoff=self.config.fuzzy_score_cutoff,
        )
        if alignment is None:
            return None
        start, end = _original_span(
            offsets,
            alignment.dest_start,
            alignment.dest_end,
            len(candidate.text),
        )
        return _TextMatch(candidate, "fuzzy", 1, float(alignment.score), start, end)

    @staticmethod
    def _match_sort_key(match: _TextMatch) -> tuple[float, ...]:
        title_bonus = 1.0 if match.candidate.node_type == "section" else 0.0
        span_length = max(1, match.end - match.start)
        query_coverage = min(1.0, span_length / max(1, len(match.candidate.text)))
        return (
            -float(match.priority),
            -match.score,
            -title_bonus,
            -query_coverage,
            float(match.candidate.order),
        )

    @staticmethod
    def _deduplicate_matches(matches: list[_TextMatch]) -> list[_TextMatch]:
        result: list[_TextMatch] = []
        seen: set[tuple[Any, ...]] = set()
        for match in matches:
            node = match.candidate.node
            element_ids = tuple(node.get("element_ids", [])) if node else ()
            matched = match.candidate.text[match.start : match.end]
            normalized, _ = _normalize_with_offsets(matched)
            key = (match.candidate.section_id, element_ids, normalized)
            if key in seen:
                continue
            seen.add(key)
            result.append(match)
        return result

    def _match_response(
        self,
        artifacts: _Artifacts,
        *,
        query: str,
        requested_mode: str,
        applied_mode: str,
        matches: list[_TextMatch],
        total_matches: int,
    ) -> DocumentAccessResponse:
        remaining = self.config.default_content_chars
        items = []
        image_paths: list[Path] = []
        for rank, match in enumerate(matches, 1):
            candidate = match.candidate
            context = ""
            if candidate.node is not None and remaining > 0:
                parent_id = candidate.node.get("parent_id")
                parent = artifacts.nodes_by_id.get(parent_id) if isinstance(parent_id, str) else None
                source = str(parent.get("text") or "") if parent else candidate.text
                per_result = min(2400, remaining)
                if len(source) <= per_result:
                    context = source
                else:
                    midpoint = (match.start + match.end) // 2
                    left = max(0, midpoint - per_result // 2)
                    context = candidate.text[left : left + per_result]
                remaining -= len(context)
            node = candidate.node or {}
            items.append(
                {
                    "rank": rank,
                    "match_type": match.match_type,
                    "score": match.score,
                    "node_id": candidate.candidate_id,
                    "node_type": candidate.node_type,
                    "section_id": candidate.section_id,
                    "section_path": candidate.section_path,
                    "page_numbers": candidate.page_numbers,
                    "matched_text": candidate.text[match.start : match.end],
                    "context_text": context,
                    "provenance": node.get("provenance", []),
                    "asset_references": node.get("asset_references", []),
                }
            )
            if candidate.node is not None:
                image_paths.extend(self._resolve_images(artifacts, candidate.node))
        return DocumentAccessResponse(
            self._base_response(artifacts)
            | {
                "operation": "find_text",
                "query": query,
                "requested_match_mode": requested_mode,
                "applied_match_mode": applied_mode,
                "matches": items,
                "result_count": len(items),
                "total_matches": total_matches,
                "truncated": total_matches > len(items),
            },
            tuple(dict.fromkeys(image_paths)),
        )

    def _section_node_ids(
        self,
        artifacts: _Artifacts,
        section_id: str,
        scope: str,
    ) -> list[str]:
        sections = self._sections_by_id(artifacts)
        if section_id not in sections:
            raise ValueError("The section does not exist.")
        if scope not in {"direct", "subtree"}:
            raise ValueError("section_scope must be direct or subtree.")
        allowed = {section_id}
        if scope == "subtree":
            pending = [section_id]
            while pending:
                children = sections[pending.pop()].get("child_section_ids", [])
                allowed.update(children)
                pending.extend(children)
        return [
            node_id
            for section in artifacts.structure.get("sections", [])
            if section.get("section_id") in allowed
            for node_id in section.get("direct_node_ids", [])
        ]

    def _page_range(
        self,
        page_start: int | None,
        page_end: int | None,
    ) -> tuple[int, int]:
        if type(page_start) is not int or page_start <= 0:
            raise ValueError("page_start must be a positive integer.")
        end = page_start if page_end is None else page_end
        if type(end) is not int or end < page_start:
            raise ValueError("page_end must be an integer at least page_start.")
        if end - page_start + 1 > self.config.max_page_span:
            raise ValueError(f"A page query may cover at most {self.config.max_page_span} pages.")
        return page_start, end

    @staticmethod
    def _pages_match(pages: list[int], page_range: tuple[int, int] | None) -> bool:
        if page_range is None:
            return True
        start, end = page_range
        return any(start <= page <= end for page in pages)

    @staticmethod
    def _sections_by_id(artifacts: _Artifacts) -> dict[str, dict[str, Any]]:
        return {
            section["section_id"]: section
            for section in artifacts.structure.get("sections", [])
        }

    @staticmethod
    def _section_path(
        sections: dict[str, dict[str, Any]],
        section_id: str,
    ) -> list[str]:
        path = []
        current: str | None = section_id
        while current:
            section = sections[current]
            path.append(str(section.get("title") or ""))
            current = section.get("parent_section_id")
        return list(reversed(path))

    @staticmethod
    def _compact_section(section: dict[str, Any] | None) -> dict[str, Any] | None:
        if section is None:
            return None
        return {
            key: section.get(key)
            for key in (
                "section_id",
                "parent_section_id",
                "title",
                "level",
                "source_level",
                "synthetic",
                "heading_page_numbers",
                "direct_content_page_range",
                "subtree_page_range",
            )
        } | {"child_count": len(section.get("child_section_ids", []))}

    @staticmethod
    def _content_item(
        node: dict[str, Any],
        text: str,
        start: int,
        end: int,
    ) -> dict[str, Any]:
        metadata = node.get("metadata", {})
        return {
            "node_id": node["node_id"],
            "node_type": node.get("node_type"),
            "section_id": metadata.get("section_id"),
            "section_path": metadata.get("section_path", []),
            "page_numbers": node.get("page_numbers", []),
            "text": text,
            "text_offset_start": start,
            "text_offset_end": end,
            "text_complete": start == 0 and end >= len(str(node.get("text") or "")),
            "provenance": node.get("provenance", []),
            "asset_references": node.get("asset_references", []),
        }

    @staticmethod
    def _base_response(artifacts: _Artifacts) -> dict[str, Any]:
        return {
            "ok": True,
            "document_id": artifacts.document_id,
            "parse_version": artifacts.parse_version,
            "index_profile": artifacts.index_profile,
        }

    @staticmethod
    def _resolve_images(artifacts: _Artifacts, node: dict[str, Any]) -> list[Path]:
        paths = []
        for reference in node.get("asset_references", []):
            visual = reference.get("visual") if isinstance(reference, dict) else None
            if not isinstance(visual, str) or not visual:
                continue
            path = (artifacts.parse_path / visual).resolve()
            if path.is_relative_to(artifacts.parse_path) and path.is_file():
                paths.append(path)
        return paths
