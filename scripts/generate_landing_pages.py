"""Bulk-regenerate Get ZEN landing pages (pillars + spokes + topic deep-spokes).

By default generates only MISSING pages, prioritized as:
  1. Country hubs (14)        — pillar pages, highest authority
  2. City guides (30)          — spokes
  3. Topic deep-spokes (330)   — programmatic SEO long-tail

Honors --budget to cap total LLM calls per run.
Honors --types to restrict to a subset (e.g. --types country,city).
Honors --web-search to enable OpenAI Responses + web_search_preview tool.
Honors --force to regenerate ALL pages, not just missing ones.

Cost guide (premium model, gpt-5.2 pricing):
  - 14 country hubs       ≈ $1.10
  - 30 city guides        ≈ $2.40
  - 330 topic deep-spokes ≈ $26
  - Full refresh          ≈ $30

Typical usage:
  python scripts/generate_landing_pages.py                          # fill all missing
  python scripts/generate_landing_pages.py --budget 14               # country hubs only
  python scripts/generate_landing_pages.py --types country,city      # skip topic spokes
  python scripts/generate_landing_pages.py --web-search              # ground with web
  python scripts/generate_landing_pages.py --force --budget 5        # regen 5 oldest
"""
from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console
from rich.table import Table

from src.landing_generator import regenerate_all
from src.models import LandingPage, SessionLocal


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--budget", type=int, default=None,
                        help="Max LLM calls per run. Default: generate everything missing.")
    parser.add_argument("--types", default="country,city,topic",
                        help="Comma-separated subset of page types to generate.")
    parser.add_argument("--web-search", action="store_true",
                        help="Use OpenAI Responses API + web_search_preview for grounding.")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate existing pages too, not just missing ones.")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    console = Console()

    if args.force:
        # Wipe existing rows for the requested types so the regenerator sees
        # them as missing and re-runs from scratch.
        types = set(s.strip() for s in args.types.split(",") if s.strip())
        session = SessionLocal()
        try:
            n = session.query(LandingPage).filter(LandingPage.page_type.in_(types)).count()
            console.print(f"[yellow]--force: deleting {n} existing rows for types {sorted(types)}[/]")
            session.query(LandingPage).filter(LandingPage.page_type.in_(types)).delete(synchronize_session=False)
            session.commit()
        finally:
            session.close()

    types = tuple(s.strip() for s in args.types.split(",") if s.strip())
    console.rule(f"[bold]Generating landing pages[/] · types={types} · budget={args.budget or 'unlimited'} · web_search={args.web_search}")

    summary = regenerate_all(
        page_types=types,
        budget=args.budget,
        use_web_search=args.web_search,
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value")
    table.add_row("generated", str(summary.get("generated", 0)))
    table.add_row("errors", str(summary.get("errors", 0)))
    table.add_row("skipped (budget)", str(summary.get("skipped", 0)))
    for ptype, n in (summary.get("by_type") or {}).items():
        table.add_row(f"  · {ptype}", str(n))
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
