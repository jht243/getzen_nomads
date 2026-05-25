"""Manual SEO audit trigger — runs the BFS audit and optionally invokes the
content fixer for flagged landing pages.

Usage:
  python scripts/seo_audit.py                       # audit, print summary + JSON
  python scripts/seo_audit.py --fix --max-fixes 3   # audit, then fix 3 worst pages
  python scripts/seo_audit.py --output audit.json   # write full report to file
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from rich.console import Console
from rich.table import Table

from src.seo.audit import audit_site


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=500)
    parser.add_argument("--start", default="/")
    parser.add_argument("--output", default=None,
                        help="Write full JSON report to this file")
    parser.add_argument("--fix", action="store_true",
                        help="Run the content fixer on flagged landing pages")
    parser.add_argument("--max-fixes", type=int, default=5)
    parser.add_argument("--web-search", action="store_true",
                        help="Use web-search grounding when fixing")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()
    logging.basicConfig(level=args.log_level, format="%(levelname)s %(name)s: %(message)s")
    console = Console()

    report = audit_site(start_paths=(args.start,), max_pages=args.max_pages)
    console.rule("[bold]Get ZEN SEO audit")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    table.add_row("Pages crawled", str(len(report.pages)))
    table.add_row("Errors",  str(report.error_count))
    table.add_row("Warnings", str(report.warn_count))
    console.print(table)

    # Top 10 by error severity
    sorted_pages = sorted(
        report.pages,
        key=lambda p: (
            -sum(1 for f in p.findings if f.severity == "error"),
            -sum(1 for f in p.findings if f.severity == "warn"),
        ),
    )
    console.print("\n[bold]Top issues by page[/]")
    for p in sorted_pages[:10]:
        ec = sum(1 for f in p.findings if f.severity == "error")
        wc = sum(1 for f in p.findings if f.severity == "warn")
        if ec == 0 and wc == 0:
            continue
        console.print(f"  [yellow]{p.path}[/]  errors={ec} warns={wc} words={p.word_count} links={p.internal_link_count}")
        for f in p.findings[:5]:
            color = "red" if f.severity == "error" else "yellow"
            console.print(f"    [{color}]{f.severity}[/] {f.rule}: {f.message}")

    if report.cross_findings:
        console.print("\n[bold]Cross-page findings[/]")
        for f in report.cross_findings:
            color = "red" if f.severity == "error" else "yellow"
            console.print(f"  [{color}]{f.severity}[/] {f.rule}: {f.message}")

    if args.output:
        with open(args.output, "w") as fp:
            json.dump(report.as_dict(), fp, indent=2)
        console.print(f"\n[green]Full report written to:[/] {args.output}")

    if args.fix:
        from src.seo.content_fixer import run_fixer
        result = run_fixer(report, max_fixes=args.max_fixes, use_web_search=args.web_search)
        console.print(f"\n[bold]Content fixer:[/] {result}")

    return 1 if report.error_count > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
