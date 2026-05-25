"""Crypto-regulation scraper — country-scoped Google News for crypto
policy, tax, and banking changes relevant to crypto-native nomads.

Coverage targets:
  - Crypto-friendly banking access
  - Capital-gains tax treatment + tax residency moves
  - Exchange licensing
  - Stablecoin and CBDC announcements
  - Capital controls that gate on-ramps
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


def _crypto_queries(country_name: str) -> list[str]:
    return [
        f'"{country_name}" crypto regulation 2026',
        f'"{country_name}" crypto tax OR capital gains',
        f'"{country_name}" bitcoin bank OR banking',
        f'"{country_name}" stablecoin OR USDT regulation',
    ]


_CROSS_CUTTING_QUERIES = (
    "crypto-friendly countries 2026 nomads",
    "crypto tax residency 2026",
    "bitcoin banking expats",
    "stablecoin regulation 2026 remote workers",
    "capital controls crypto on-ramp",
)


class CryptoRegulationsScraper(BaseScraper):
    def __init__(self, *, country_names: Optional[list[str]] = None,
                 country_slugs: Optional[list[str]] = None) -> None:
        super().__init__()
        self.client.close()
        self.client = build_topical_client()
        self._country_names = country_names or []
        self._country_slugs = country_slugs or []

    def get_source_id(self) -> str:
        return "crypto_regulations"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        articles: list[ScrapedArticle] = []
        seen: set[str] = set()

        for q in _CROSS_CUTTING_QUERIES:
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                break
            for art in fetch_google_news_query(self.client, q, max_items=15):
                if art.source_url in seen:
                    continue
                seen.add(art.source_url)
                art.topic_hint = "crypto"
                articles.append(art)

        for slug, name in zip(self._country_slugs, self._country_names):
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                logger.warning("crypto_regulations: deadline hit before %s", slug)
                break
            for q in _crypto_queries(name):
                if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                    break
                for art in fetch_google_news_query(self.client, q, max_items=10):
                    if art.source_url in seen:
                        continue
                    seen.add(art.source_url)
                    art.country_hint = slug
                    art.topic_hint = "crypto"
                    articles.append(art)

        elapsed = int(time.monotonic() - start)
        logger.info("crypto_regulations: %d unique articles in %ds", len(articles), elapsed)
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )
