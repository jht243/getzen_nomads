"""Visa-portal scraper — country-scoped Google News for visa policy changes.

Most government immigration portals don't publish RSS, so the highest-yield
signal is filtered news coverage that surfaces program announcements,
fee changes, eligibility updates, and processing-time shifts.

Queries are tighter than the general nomad scraper — limited to visa,
residency, and immigration policy terms with explicit time-windowing.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

from src.config import settings
from src.scraper.base import BaseScraper, ScrapedArticle, ScrapeResult
from src.scraper.google_news import build_topical_client, fetch_google_news_query

logger = logging.getLogger(__name__)

_HARD_DEADLINE_SECONDS = 90


def _visa_queries(country_name: str) -> list[str]:
    """Per-country visa-policy query set."""
    return [
        f'"{country_name}" digital nomad visa {date.today().year}',
        f'"{country_name}" visa policy change OR new program',
        f'"{country_name}" residency permit nomads',
        f'"{country_name}" visa fee OR processing time',
        f'"{country_name}" e-visa update',
    ]


_CROSS_CUTTING_QUERIES = (
    "digital nomad visa launched OR approved 2026",
    "new remote work visa program",
    "visa fee increase 2026 expat",
    "residency by investment 2026 nomads",
)


class VisaPortalsScraper(BaseScraper):
    def __init__(self, *, country_names: Optional[list[str]] = None,
                 country_slugs: Optional[list[str]] = None) -> None:
        super().__init__()
        self.client.close()
        self.client = build_topical_client()
        self._country_names = country_names or []
        self._country_slugs = country_slugs or []

    def get_source_id(self) -> str:
        return "visa_portal"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        articles: list[ScrapedArticle] = []
        seen: set[str] = set()

        # Cross-cutting first
        for q in _CROSS_CUTTING_QUERIES:
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                break
            for art in fetch_google_news_query(self.client, q, max_items=15):
                if art.source_url in seen:
                    continue
                seen.add(art.source_url)
                art.topic_hint = "visa"
                art.article_type = "policy_news"
                articles.append(art)

        # Per-country (use rotation? for visa it makes more sense to cover
        # all countries every run since policy changes are infrequent —
        # but keep wall-clock bounded).
        for slug, name in zip(self._country_slugs, self._country_names):
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                logger.warning("visa_portals: deadline hit before %s", slug)
                break
            for q in _visa_queries(name):
                if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                    break
                for art in fetch_google_news_query(self.client, q, max_items=10):
                    if art.source_url in seen:
                        continue
                    seen.add(art.source_url)
                    art.country_hint = slug
                    art.topic_hint = "visa"
                    art.article_type = "policy_news"
                    articles.append(art)

        elapsed = int(time.monotonic() - start)
        logger.info("visa_portals: %d unique articles in %ds", len(articles), elapsed)
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )
