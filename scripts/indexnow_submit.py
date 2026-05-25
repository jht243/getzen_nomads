"""One-shot IndexNow submission — supply URLs via stdin, file, or
--all-sitemap to submit every URL currently in the canonical sitemap.

Usage:
  echo "https://www.getzen.cash/colombia/" | python scripts/indexnow_submit.py
  python scripts/indexnow_submit.py --file urls.txt
  python scripts/indexnow_submit.py --all-sitemap
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Iterable

from rich.console import Console


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", default=None, help="Read URLs from file (one per line)")
    parser.add_argument("--all-sitemap", action="store_true",
                        help="Submit every URL in the local sitemap.xml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    console = Console()

    from src.distribution import indexnow

    urls: list[str] = []
    if args.all_sitemap:
        from scripts.sync_sitemap import _local_sitemap_urls
        urls = _local_sitemap_urls()
    elif args.file:
        urls = [line.strip() for line in Path(args.file).read_text().splitlines() if line.strip()]
    else:
        urls = [line.strip() for line in sys.stdin.readlines() if line.strip()]

    if not urls:
        console.print("[yellow]No URLs to submit[/]")
        return 0

    console.print(f"Submitting [bold]{len(urls)}[/] URLs to IndexNow…")
    result = indexnow.submit_urls(urls)
    console.print(f"Result: success={result.success} status={result.status_code} submitted={result.submitted}")
    if result.response_snippet:
        console.print(f"Body: {result.response_snippet[:200]}")
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
