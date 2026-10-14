"""Command-line entry point for prepared-index inspection."""

import argparse
import json
from pathlib import Path

from opentalk.documents.index_inspection import inspect_prepared_index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and summarize one prepared indexing profile."
    )
    parser.add_argument(
        "index_path",
        type=Path,
        help="Path to a parse's indexing/<profile> directory.",
    )
    parser.add_argument(
        "--sample-size",
        type=int,
        default=0,
        help="Include previews for this many indexable nodes.",
    )
    args = parser.parse_args()
    report = inspect_prepared_index(args.index_path, sample_size=args.sample_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["valid"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
