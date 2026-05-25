"""Sitemap sync — compare server-generated sitemap.xml against the live site
and patch any missing routes.

Strategy:
  1. Render the current sitemap.xml via Flask test_client (canonical truth).
  2. Optionally fetch the live sitemap.xml from `--live-url` and diff URL sets.
  3. Print a missing/extra report.
  4. With --submit, ping IndexNow + Google Indexing for the missing URLs.

If --spot-check is set, GETs up to N random URLs from the canonical sitemap
to confirm they return 200 — flags 404s/5xx so we know our sitemap is honest.
"""
from __future__ import annotations

import argparse
import json
import logging
import random
import re
import sys
import xml.etree.ElementTree as ET
from typing import Iterable

import httpx
from rich.console import Console
from rich.table import Table


_SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def _parse_sitemap(body: str) -> list[str]:
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise ValueError(f"Sitemap XML parse failed: {exc}")
    return [el.text.strip() for el in root.findall(".//sm:url/sm:loc", _SITEMAP_NS) if el.text]


def _local_sitemap_urls() -> list[str]:
    from server import app
    client = app.test_client()
    resp = client.get("/sitemap.xml")
    if resp.status_code != 200:
        raise RuntimeError(f"Local sitemap returned {resp.status_code}")
    return _parse_sitemap(resp.get_data(as_text=True))


def _live_sitemap_urls(live_url: str) -> list[str]:
    resp = httpx.get(live_url, timeout=15, follow_redirects=True)
    resp.raise_for_status()
    return _parse_sitemap(resp.text)


def _spot_check(urls: Iterable[str], *, n: int) -> list[tuple[str, int]]:
    urls = list(urls)
    sample = random.sample(urls, min(n, len(urls)))
    results: list[tuple[str, int]] = []
    from server import app
    client = app.test_client()
    for url in sample:
        # Convert absolute URL back to path for test_client
        path = re.sub(r"^https?://[^/]+", "", url) or "/"
        try:
            r = client.get(path)
            results.append((url, r.status_code))
        except Exception as exc:
            results.append((url, 0))
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-url", default=None,
                        help="Fetch this URL and diff against the local sitemap")
    parser.add_argument("--spot-check", type=int, default=0,
                        help="GET N random URLs to verify they're 200")
    parser.add_argument("--submit", action="store_true",
                        help="Ping IndexNow + Google Indexing for missing URLs")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    console = Console()

    local = _local_sitemap_urls()
    console.print(f"[green]Local sitemap:[/] {len(local)} URLs")

    diff_missing: list[str] = []
    diff_extra: list[str] = []
    if args.live_url:
        try:
            live = _live_sitemap_urls(args.live_url)
            console.print(f"[green]Live sitemap:[/] {len(live)} URLs ({args.live_url})")
            local_set, live_set = set(local), set(live)
            diff_missing = sorted(local_set - live_set)
            diff_extra = sorted(live_set - local_set)
            table = Table()
            table.add_column("Diff")
            table.add_column("Count")
            table.add_row("In local, missing from live", str(len(diff_missing)))
            table.add_row("In live, not in local", str(len(diff_extra)))
            console.print(table)
            for url in diff_missing[:20]:
                console.print(f"  [yellow]missing[/] {url}")
            for url in diff_extra[:20]:
                console.print(f"  [dim]extra[/]   {url}")
        except Exception as exc:
            console.print(f"[red]Live fetch failed:[/] {exc}")

    if args.spot_check:
        results = _spot_check(local, n=args.spot_check)
        ok = sum(1 for _, s in results if s == 200)
        bad = [(u, s) for u, s in results if s != 200]
        console.print(f"[green]Spot check:[/] {ok}/{len(results)} returned 200")
        for url, status in bad:
            console.print(f"  [red]{status}[/] {url}")

    if args.submit and diff_missing:
        from src.distribution import indexnow, runner as dist_runner
        result = indexnow.submit_urls(diff_missing[:500])
        console.print(f"[green]IndexNow:[/] {result.success} ({result.submitted} URLs, code {result.status_code})")
        # Google Indexing API submission goes through the regular runner so
        # the cooldown + log tables stay consistent.
        gi_result = dist_runner.run_google_indexing()
        console.print(f"[green]Google Indexing:[/] {gi_result}")

    return 0 if not args.live_url or not diff_missing else 1


if __name__ == "__main__":
    sys.exit(main())
