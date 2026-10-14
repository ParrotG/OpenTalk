"""Command-line entry point for hierarchical index preparation."""

import argparse
import json
from pathlib import Path

from opentalk.documents.config import load_indexing_config
from opentalk.documents.indexing import prepare_index


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare hierarchical indexing nodes from one completed PDF parse."
    )
    parser.add_argument("parse_path", type=Path, help="Path to a completed parse directory.")
    parser.add_argument("--config", type=Path, help="Document configuration path.")
    parser.add_argument("--profile", help="Override the indexing profile name for this run.")
    args = parser.parse_args()
    result = prepare_index(
        args.parse_path,
        config=load_indexing_config(args.config),
        profile=args.profile,
    )
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
