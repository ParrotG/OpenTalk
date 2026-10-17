"""Command-line access to prepared document structure and text."""

import argparse
import json
import sys

from opentalk.documents.access import DocumentAccessService


def main() -> None:
    parser = argparse.ArgumentParser(description="Browse or find text in a prepared PDF index.")
    subparsers = parser.add_subparsers(dest="operation", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Browse an outline, section, or page.")
    inspect_parser.add_argument("document_id")
    inspect_parser.add_argument("--view", choices=("outline", "section", "page"), required=True)
    inspect_parser.add_argument("--section-id")
    inspect_parser.add_argument("--parent-section-id")
    inspect_parser.add_argument("--section-scope", choices=("direct", "subtree"), default="direct")
    inspect_parser.add_argument("--page-start", type=int)
    inspect_parser.add_argument("--page-end", type=int)
    inspect_parser.add_argument("--cursor", default="")
    inspect_parser.add_argument("--max-items", type=int)
    inspect_parser.add_argument("--max-chars", type=int)

    find_parser = subparsers.add_parser("find", help="Find exact or fuzzy text.")
    find_parser.add_argument("document_id")
    find_parser.add_argument("query")
    find_parser.add_argument("--match-mode", choices=("auto", "substring", "fuzzy"), default="auto")
    find_parser.add_argument("--section-id")
    find_parser.add_argument("--page-start", type=int)
    find_parser.add_argument("--page-end", type=int)
    find_parser.add_argument("--limit", type=int)

    for operation_parser in (inspect_parser, find_parser):
        operation_parser.add_argument("--parse-version")
        operation_parser.add_argument("--index-profile")
    args = parser.parse_args()
    service = DocumentAccessService()
    try:
        if args.operation == "inspect":
            response = service.inspect(
                args.document_id,
                view=args.view,
                section_id=args.section_id,
                parent_section_id=args.parent_section_id,
                section_scope=args.section_scope,
                page_start=args.page_start,
                page_end=args.page_end,
                cursor=args.cursor,
                max_items=args.max_items,
                max_chars=args.max_chars,
                parse_version=args.parse_version,
                index_profile=args.index_profile,
            )
        else:
            response = service.find_text(
                args.document_id,
                args.query,
                match_mode=args.match_mode,
                section_id=args.section_id,
                page_start=args.page_start,
                page_end=args.page_end,
                limit=args.limit,
                parse_version=args.parse_version,
                index_profile=args.index_profile,
            )
        print(json.dumps(response.to_dict(include_local_paths=True), ensure_ascii=False, indent=2))
    except ValueError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
