"""Command-line entry point for the complete uploaded-document pipeline."""

import argparse
import json
from pathlib import Path

from opentalk.documents.pipeline import process_pdf


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse, prepare, embed, and persist one PDF document."
    )
    parser.add_argument("pdf", type=Path, help="Path to the uploaded PDF file.")
    args = parser.parse_args()
    print(json.dumps(process_pdf(args.pdf).to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
