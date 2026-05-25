"""Get ZEN daily orchestrator.

Phases (each isolated — a failure in one does NOT halt the rest):
  1. Scrape — pull articles from configured scrapers
  2. Analyze — LLM nomad-relevance scoring + country/city/topic extraction
  3. Generate briefings — high-relevance articles → BlogPost
  4. (TODO) OG image generation, sitemap sync, IndexNow, Google Indexing,
     SEO audit, content fixer, newsletter — wired in subsequent phases.

CLI flags:
  --skip-scrape    Use existing DB data, skip the network round-trip
  --skip-analyze   Don't run the LLM analyzer
  --skip-blog      Don't generate briefings
  --dry-run        Implies --skip-scrape and --skip-blog (analyzer still runs
                   if API key is set; safe because budget caps cost it).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date

from rich.console import Console
from rich.table import Table

from src.config import settings


console = Console()


def _phase(name: str):
    console.rule(f"[bold cyan]{name}[/]")
    return time.monotonic()


def _phase_end(start: float, summary: dict | None = None) -> float:
    elapsed = time.monotonic() - start
    console.print(f"[green]✔[/] done in {elapsed:.1f}s")
    if summary:
        console.print(summary)
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-scrape", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    parser.add_argument("--skip-blog", action="store_true")
    parser.add_argument("--skip-distribute", action="store_true",
                        help="Skip IndexNow + Google Indexing submissions")
    parser.add_argument("--skip-audit", action="store_true",
                        help="Skip the SEO audit phase")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=settings.log_level)
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(levelname)s %(name)s: %(message)s",
    )
    target = date.today()
    console.print(f"[bold]Get ZEN daily pipeline[/] — {target}")

    if args.dry_run:
        args.skip_scrape = True
        args.skip_blog = True
        args.skip_distribute = True
        console.print("[yellow]Dry run: skipping scrape + blog + distribute[/]")

    results: dict[str, dict] = {}

    # ── Phase 1: Scrape ───────────────────────────────────────────────────
    if not args.skip_scrape:
        start = _phase("Phase 1: Scrape")
        try:
            from src.pipeline import run_scrapers
            results["scrape"] = run_scrapers(target_date=target)
        except Exception as exc:
            console.print(f"[red]Scrape phase failed:[/] {exc}")
            results["scrape"] = {"error": str(exc)}
        _phase_end(start, results.get("scrape"))
    else:
        console.print("[dim]Skipping Phase 1 (scrape)[/]")

    # ── Phase 2: Analyze ──────────────────────────────────────────────────
    if not args.skip_analyze:
        start = _phase("Phase 2: Analyze")
        try:
            from src.analyzer import run_analysis
            results["analyze"] = run_analysis()
        except Exception as exc:
            console.print(f"[red]Analyze phase failed:[/] {exc}")
            results["analyze"] = {"error": str(exc)}
        _phase_end(start, results.get("analyze"))
    else:
        console.print("[dim]Skipping Phase 2 (analyze)[/]")

    # ── Phase 3: Briefing generation ──────────────────────────────────────
    if not args.skip_blog:
        start = _phase("Phase 3: Briefing generation")
        try:
            from src.blog_generator import run_blog_generation
            results["blog"] = run_blog_generation()
        except Exception as exc:
            console.print(f"[red]Blog phase failed:[/] {exc}")
            results["blog"] = {"error": str(exc)}
        _phase_end(start, results.get("blog"))
    else:
        console.print("[dim]Skipping Phase 3 (blog)[/]")

    # ── Phase 4: Distribution (IndexNow + Google Indexing) ───────────────
    if not args.skip_distribute:
        start = _phase("Phase 4: Distribution")
        try:
            from src.distribution.runner import run_all as run_distribution
            results["distribute"] = run_distribution()
        except Exception as exc:
            console.print(f"[red]Distribution phase failed:[/] {exc}")
            results["distribute"] = {"error": str(exc)}
        _phase_end(start, results.get("distribute"))
    else:
        console.print("[dim]Skipping Phase 4 (distribute)[/]")

    # ── Phase 5: SEO audit ───────────────────────────────────────────────
    if not args.skip_audit:
        start = _phase("Phase 5: SEO audit")
        try:
            from src.seo.audit import audit_site
            report = audit_site(max_pages=200)
            results["audit"] = {
                "pages": len(report.pages),
                "errors": report.error_count,
                "warns": report.warn_count,
            }
        except Exception as exc:
            console.print(f"[red]Audit phase failed:[/] {exc}")
            results["audit"] = {"error": str(exc)}
        _phase_end(start, results.get("audit"))
    else:
        console.print("[dim]Skipping Phase 5 (audit)[/]")

    # ── Summary ───────────────────────────────────────────────────────────
    console.rule("[bold green]Pipeline summary")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Phase")
    table.add_column("Result")
    for k, v in results.items():
        table.add_row(k, str(v))
    console.print(table)
    return 0


if __name__ == "__main__":
    sys.exit(main())
