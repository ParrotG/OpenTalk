"""Command-line entry point for versioned Docling PDF parsing."""

import argparse
import json
from pathlib import Path

from opentalk.documents.config import load_document_config
from opentalk.documents.parser import parse_pdf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse one PDF with Docling and save versioned inspection artifacts."
    )
    parser.add_argument("pdf", type=Path, help="Path to the PDF file.")
    parser.add_argument("--config", type=Path, help="Document parser configuration path.")
    parser.add_argument(
        "--storage-root",
        type=Path,
        help="Override the global document storage root for this run.",
    )
    args = parser.parse_args()
    configuration = load_document_config(args.config)
    if args.storage_root is not None:
        from dataclasses import replace

        configuration = replace(configuration, storage_root=args.storage_root.resolve())
    result = parse_pdf(args.pdf, config=configuration)
    print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
