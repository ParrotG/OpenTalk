"""Command-line entry point for inspectable hybrid document retrieval."""

import argparse
import json
import sys

from opentalk.documents.retrieval import LazyRetrievalService


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run hybrid retrieval with hierarchical context expansion."
    )
    parser.add_argument("query", help="Standalone query to search for.")
    parser.add_argument("--alternate-query", help="Optional alternate wording for query fusion.")
    parser.add_argument("--document-id")
    parser.add_argument("--parse-version")
    parser.add_argument("--index-profile")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    service = LazyRetrievalService()
    try:
        response = service.search(
            args.query,
            alternate_query=args.alternate_query,
            document_id=args.document_id,
            parse_version=args.parse_version,
            index_profile=args.index_profile,
            limit=args.limit,
        )
        print(json.dumps(response.to_dict(include_local_paths=True), ensure_ascii=False, indent=2))
    except (RuntimeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        print(
            f"Retrieval failed ({type(error).__name__}). Check Qdrant connectivity and logs.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    finally:
        service.close()


if __name__ == "__main__":
    main()
