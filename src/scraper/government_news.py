"""Government-program scraper — country-scoped Google News for new programs,
tax residency changes, e-Residency announcements, and bilateral agreements.

Distinct from visa_portals.py in that this scraper tracks the wider gov
policy landscape that affects nomads — capital controls, dollarization,
foreign-worker programs, double-taxation treaties — not just visa rules.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

from src.scraper.base import BaseScraper, ScrapedArticle, ScrapeResult
from src.scraper.google_news import build_topical_client, fetch_google_news_query

logger = logging.getLogger(__name__)

_HARD_DEADLINE_SECONDS = 80


def _gov_queries(country_name: str) -> list[str]:
    return [
        f'"{country_name}" tax residency program 2026',
        f'"{country_name}" e-residency OR e-resident launch',
        f'"{country_name}" foreign worker permit',
        f'"{country_name}" double taxation treaty',
        f'"{country_name}" capital controls OR dollarization 2026',
    ]


_CROSS_CUTTING_QUERIES = (
    "new e-residency country 2026",
    "tax residency change 2026 nomads",
    "double taxation treaty 2026 remote workers",
    "foreign-worker permit launch",
    "capital controls 2026 expats",
)


class GovernmentNewsScraper(BaseScraper):
    def __init__(self, *, country_names: Optional[list[str]] = None,
                 country_slugs: Optional[list[str]] = None) -> None:
        super().__init__()
        self.client.close()
        self.client = build_topical_client()
        self._country_names = country_names or []
        self._country_slugs = country_slugs or []

    def get_source_id(self) -> str:
        return "government_news"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        articles: list[ScrapedArticle] = []
        seen: set[str] = set()

        for q in _CROSS_CUTTING_QUERIES:
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                break
            for art in fetch_google_news_query(self.client, q, max_items=12):
                if art.source_url in seen:
                    continue
                seen.add(art.source_url)
                articles.append(art)

        for slug, name in zip(self._country_slugs, self._country_names):
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                logger.warning("government_news: deadline hit before %s", slug)
                break
            for q in _gov_queries(name):
                if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                    break
                for art in fetch_google_news_query(self.client, q, max_items=8):
                    if art.source_url in seen:
                        continue
                    seen.add(art.source_url)
                    art.country_hint = slug
                    articles.append(art)

        elapsed = int(time.monotonic() - start)
        logger.info("government_news: %d unique articles in %ds", len(articles), elapsed)
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )
