"""Base scraper — retry-aware httpx client + standard result types.

Adapted from ban_the_bots/src/scraper/base.py, stripped to nomad-relevant
content (no gazette/PDF helpers).
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class ScrapedArticle:
    """Article from any external source — Google News, visa portal, advisory, etc."""

    headline: str
    published_date: date
    source_url: str
    body_text: Optional[str] = None
    source_name: str = ""
    source_credibility: str = "tier2"
    article_type: str = "news"
    # Geographic hints from the scraper. The analyzer resolves these
    # to country_id/city_id/topic_id on persisted rows.
    country_hint: Optional[str] = None
    city_hint: Optional[str] = None
    topic_hint: Optional[str] = None
    extra_metadata: dict = field(default_factory=dict)


@dataclass
class ScrapeResult:
    source: str
    success: bool
    articles: list[ScrapedArticle] = field(default_factory=list)
    error: Optional[str] = None
    duration_seconds: int = 0


class BaseScraper(ABC):
    def __init__(self) -> None:
        self.client = httpx.Client(
            timeout=settings.scraper_timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    @abstractmethod
    def get_source_id(self) -> str: ...

    @abstractmethod
    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult: ...

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    @retry(
        stop=stop_after_attempt(settings.scraper_max_retries),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        retry=retry_if_exception_type(
            (httpx.HTTPStatusError, httpx.ConnectError, httpx.TimeoutException)
        ),
    )
    def _fetch(self, url: str, params: Optional[dict] = None) -> httpx.Response:
        logger.info("Fetching %s", url)
        resp = self.client.get(url, params=params)
        resp.raise_for_status()
        return resp
