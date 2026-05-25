"""Audit-driven content fixer.

Reads an AuditReport (from src/seo/audit.py), groups findings by URL, and
regenerates the underlying LandingPage row when the page is a known
hub/spoke/topic landing page with fixable issues:

  - thin_content        → regenerate with stronger word-count target
  - missing_h1          → regenerate (body never has h1 — template emits it,
                          so this is a template-level bug, not fixable here)
  - heading_skip        → regenerate
  - missing_jsonld      → not fixable here (template renders JSON-LD; missing
                          implies the page has no LandingPage row yet —
                          generate fresh)
  - missing_description → regenerate so the generator produces a new summary
  - missing_cluster_nav → not fixable here (template-level; surface as warning)

Briefings are NOT auto-fixed — they're tied to a source article and we'd
rather regenerate the whole briefing in the daily pipeline than patch it.

Hard cap on fixes per run via `--max-fixes` to keep cost predictable.
"""
from __future__ import annotations

import logging
from typing import Iterable

from src.config import settings
from src.landing_generator import (
    generate_country_hub,
    generate_city_guide,
    generate_topic_page,
)
from src.seo.audit import AuditReport, Finding, SEVERITY_ERROR, SEVERITY_WARN

logger = logging.getLogger(__name__)

# Findings that this fixer can correct by regenerating the landing page.
_FIXABLE_RULES = {
    "thin_content",
    "heading_skip",
    "missing_description",
    "description_too_short",
    "description_too_long",
    "title_too_short",
    "title_too_long",
    "missing_jsonld",
}


def _classify_path(path: str) -> tuple[str, tuple]:
    """Return (page_type, slug_tuple) for a landing-page URL, else ("", ())."""
    parts = [p for p in path.strip("/").split("/") if p]
    if len(parts) == 1:
        return "country", (parts[0],)
    if len(parts) == 2:
        return "city", (parts[0], parts[1])
    if len(parts) == 3:
        return "topic", (parts[0], parts[1], parts[2])
    return "", ()


def fixable_pages(report: AuditReport) -> list[tuple[str, str, tuple, list[Finding]]]:
    """Return [(path, page_type, slug_tuple, findings)] for pages we can fix."""
    out: list[tuple[str, str, tuple, list[Finding]]] = []
    for page in report.pages:
        page_type, slugs = _classify_path(page.path)
        if page_type not in ("country", "city", "topic"):
            continue
        fixable = [
            f for f in page.findings
            if f.severity in (SEVERITY_ERROR, SEVERITY_WARN) and f.rule in _FIXABLE_RULES
        ]
        if fixable:
            out.append((page.path, page_type, slugs, fixable))
    return out


def run_fixer(
    report: AuditReport,
    *,
    max_fixes: int = 5,
    use_web_search: bool = False,
) -> dict:
    """Regenerate landing pages flagged with fixable findings."""
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — cannot run content fixer")
        return {"status": "skipped", "fixed": 0}

    targets = fixable_pages(report)
    if not targets:
        return {"status": "ok", "fixed": 0, "candidates": 0}

    targets.sort(key=lambda t: -len([f for f in t[3] if f.severity == SEVERITY_ERROR]))

    summary = {"fixed": 0, "errors": 0, "skipped": 0, "candidates": len(targets), "by_type": {}}
    for path, page_type, slugs, findings in targets[:max_fixes]:
        rules = sorted({f.rule for f in findings})
        try:
            logger.info("Fixing %s (type=%s, rules=%s)", path, page_type, rules)
            if page_type == "country":
                generate_country_hub(slugs[0], use_web_search=use_web_search)
            elif page_type == "city":
                generate_city_guide(slugs[0], slugs[1], use_web_search=use_web_search)
            elif page_type == "topic":
                generate_topic_page(slugs[0], slugs[1], slugs[2], use_web_search=use_web_search)
            summary["fixed"] += 1
            summary["by_type"][page_type] = summary["by_type"].get(page_type, 0) + 1
        except Exception as exc:
            logger.exception("Fixer failed for %s: %s", path, exc)
            summary["errors"] += 1

    if len(targets) > max_fixes:
        summary["skipped"] = len(targets) - max_fixes
    logger.info("Content fixer summary: %s", summary)
    return {"status": "ok", **summary}
