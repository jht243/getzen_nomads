"""Google News RSS scraper for nomad/expat/visa/cost/safety topics.

Rotation strategy: don't query every country every run. Rotate `country_rotation_per_run`
countries each invocation so a country is scraped 2-3x/week. Cross-cutting
queries (no country) run every time.

Failure contract: bounded retries, hard wall-clock budget, returns whatever
it has on partial failure with success=True.
"""
from __future__ import annotations

import json
import logging
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import httpx

from src.config import settings
from src.scraper.base import BaseScraper, ScrapedArticle, ScrapeResult

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

MAX_ITEMS_PER_QUERY = 20
MAX_TOTAL_WALLCLOCK_SECONDS = 120
PER_QUERY_BACKOFF_SECONDS = 5
PER_QUERY_MAX_ATTEMPTS = 2


# Cross-cutting queries — always run, no country binding
CROSS_CUTTING_QUERIES: tuple[str, ...] = (
    '"digital nomad visa" 2026',
    '"remote work visa" new program',
    '"crypto friendly countries" 2026',
    '"cheapest cities remote workers"',
    '"safest countries digital nomads"',
    '"emerging destinations remote workers"',
    '"frontier markets expat"',
    '"nomad visa" approved OR launched',
    '"long stay visa" remote workers',
    '"digital nomad" tax residency',
)


HIGH_CREDIBILITY = {
    "reuters.com", "apnews.com", "bloomberg.com", "ft.com", "wsj.com",
    "fortune.com", "bbc.com", "theguardian.com", "nytimes.com",
    "washingtonpost.com", "economist.com", "aljazeera.com",
    "nomadcapitalist.com", "internationalliving.com",
}

_PUBLISHER_DOMAIN_ALIASES: dict[str, str] = {
    "reuters": "reuters.com",
    "ap news": "apnews.com",
    "bloomberg": "bloomberg.com",
    "financial times": "ft.com",
    "the wall street journal": "wsj.com",
    "the new york times": "nytimes.com",
    "the washington post": "washingtonpost.com",
    "bbc": "bbc.com",
    "bbc news": "bbc.com",
    "the economist": "economist.com",
    "the guardian": "theguardian.com",
    "al jazeera": "aljazeera.com",
    "nomad capitalist": "nomadcapitalist.com",
    "international living": "internationalliving.com",
}


# ── Rotation state ────────────────────────────────────────────────────────
_ROTATION_FILE = Path("./storage/rotation_state.json")


def _load_rotation_state() -> dict:
    if not _ROTATION_FILE.exists():
        return {"cursor": 0}
    try:
        return json.loads(_ROTATION_FILE.read_text())
    except Exception:
        return {"cursor": 0}


def _save_rotation_state(state: dict) -> None:
    _ROTATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ROTATION_FILE.write_text(json.dumps(state))


def _select_rotation(country_slugs: list[str], n: int) -> tuple[list[str], int]:
    """Pick the next `n` country slugs in a round-robin cursor over the list."""
    if not country_slugs:
        return [], 0
    state = _load_rotation_state()
    cursor = int(state.get("cursor", 0)) % len(country_slugs)
    take = max(1, min(n, len(country_slugs)))
    picks = [country_slugs[(cursor + i) % len(country_slugs)] for i in range(take)]
    new_cursor = (cursor + take) % len(country_slugs)
    _save_rotation_state({"cursor": new_cursor})
    return picks, new_cursor


# ── Per-country / per-city query construction ─────────────────────────────
def _country_queries(country_name: str) -> list[str]:
    name = country_name
    return [
        f'"digital nomad {name}" 2026',
        f'"remote work {name}" 2026',
        f'"{name} digital nomad visa"',
        f'"{name} safety travel" OR "{name} crime advisory"',
        f'"cost of living {name}" 2026',
        f'"crypto {name}" OR "{name} crypto regulation"',
        f'"internet {name}" speed OR connectivity',
        f'"coworking {name}"',
        f'"expat {name}" 2026',
    ]


def _city_queries(city_name: str, country_name: str) -> list[str]:
    return [
        f'"{city_name}" remote work OR digital nomad',
        f'"{city_name} {country_name}" expat',
        f'"{city_name}" coworking OR wifi',
    ]


# ──────────────────────────────────────────────────────────────────────────
def fetch_google_news_query(
    client: httpx.Client,
    query: str,
    *,
    max_items: int = MAX_ITEMS_PER_QUERY,
    max_attempts: int = PER_QUERY_MAX_ATTEMPTS,
    backoff_seconds: int = PER_QUERY_BACKOFF_SECONDS,
) -> list[ScrapedArticle]:
    """Reusable Google News RSS query — shared by every scraper subclass.

    Caller owns the httpx client (so it can configure timeouts, headers,
    rate-limit pooling). Returns parsed ScrapedArticle list; partial /
    total failure returns []."""
    params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    body: Optional[str] = None
    for attempt in range(max_attempts):
        try:
            resp = client.get(GOOGLE_NEWS_RSS, params=params)
            resp.raise_for_status()
            body = resp.text
            break
        except (
            httpx.ConnectError, httpx.TimeoutException,
            httpx.RemoteProtocolError, httpx.HTTPStatusError, ssl.SSLError,
        ) as exc:
            logger.warning(
                "Google News query %r attempt %d/%d failed (%s)",
                query[:40], attempt + 1, max_attempts, type(exc).__name__,
            )
            if attempt + 1 < max_attempts:
                time.sleep(backoff_seconds)
    if not body:
        return []
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        logger.warning("Google News query %r returned non-XML: %s", query[:40], exc)
        return []

    out: list[ScrapedArticle] = []
    for item in root.findall(".//item")[:max_items]:
        parsed = _parse_google_news_item(item)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_google_news_item(item) -> Optional[ScrapedArticle]:
    """Standalone parser used by both GoogleNewsScraper and topic subclasses."""
    try:
        title_full = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_str = (item.findtext("pubDate") or "").strip()
        desc = item.findtext("description") or ""
        if not title_full or not link:
            return None
        title = title_full
        publisher = ""
        if " - " in title_full:
            title, publisher = title_full.rsplit(" - ", 1)
            title = title.strip()
            publisher = publisher.strip()

        pub_date = _parse_rfc822_date(pub_str) or date.today()
        publisher_domain = _publisher_to_domain_static(publisher)

        return ScrapedArticle(
            headline=title,
            published_date=pub_date,
            source_url=link,
            body_text=None,
            source_name="Google News",
            source_credibility=_infer_credibility_static(publisher_domain),
            article_type="news",
            extra_metadata={
                "publisher": publisher,
                "publisher_domain": publisher_domain,
                "snippet": _strip_html_static(desc)[:240],
                "query_via": "google_news_rss",
            },
        )
    except Exception as exc:
        logger.debug("Google News item parse error: %s", exc)
        return None


def _parse_rfc822_date(s: str) -> Optional[date]:
    if not s:
        return None
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
        try:
            return datetime.strptime(s.strip()[:31], fmt).date()
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:25], "%a, %d %b %Y %H:%M:%S").date()
    except ValueError:
        return None


def _publisher_to_domain_static(publisher: str) -> str:
    if not publisher:
        return ""
    slug = publisher.lower().strip()
    return _PUBLISHER_DOMAIN_ALIASES.get(slug, slug)


def _infer_credibility_static(domain: str) -> str:
    d = (domain or "").lower()
    if any(h in d for h in HIGH_CREDIBILITY):
        return "tier1"
    return "tier2"


def _strip_html_static(s: str) -> str:
    if not s:
        return ""
    text = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", text).strip()


def build_topical_client() -> httpx.Client:
    """Shared httpx client config for any Google-News-backed scraper."""
    return httpx.Client(
        timeout=httpx.Timeout(15.0, connect=8.0),
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


class GoogleNewsScraper(BaseScraper):
    """Topical news from Google News RSS, country-rotated."""

    def __init__(
        self,
        *,
        country_slugs: Optional[list[str]] = None,
        country_name_by_slug: Optional[dict[str, str]] = None,
        city_name_by_slug: Optional[dict[tuple[str, str], str]] = None,
        rotation_n: Optional[int] = None,
    ) -> None:
        super().__init__()
        self.client.close()
        self.client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=8.0),
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
        self._country_slugs = country_slugs or []
        self._country_name_by_slug = country_name_by_slug or {}
        self._city_name_by_slug = city_name_by_slug or {}
        self._rotation_n = rotation_n or settings.country_rotation_per_run

    def get_source_id(self) -> str:
        return "google_news"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        deadline = start + MAX_TOTAL_WALLCLOCK_SECONDS

        # Pick countries for this run
        picked, _ = _select_rotation(self._country_slugs, self._rotation_n)
        logger.info("Google News rotation: %s", picked)

        # Build query list: cross-cutting + per-country (and a few per-city)
        queries: list[tuple[str, Optional[str], Optional[str]]] = []  # (q, country_slug, city_slug)
        for q in CROSS_CUTTING_QUERIES:
            queries.append((q, None, None))
        for slug in picked:
            country_name = self._country_name_by_slug.get(slug, slug.replace("-", " ").title())
            for q in _country_queries(country_name):
                queries.append((q, slug, None))
            # Add up to 2 cities per country for finer hints
            cities_for_country = [
                (cslug, cname)
                for (csl, cslug), cname in self._city_name_by_slug.items()
                if csl == slug
            ]
            for cslug, cname in cities_for_country[:2]:
                for q in _city_queries(cname, country_name):
                    queries.append((q, slug, cslug))

        seen_urls: set[str] = set()
        articles: list[ScrapedArticle] = []
        run_count = 0

        # Cap absolute number of queries to keep wall-clock predictable.
        for q, country_slug, city_slug in queries[:60]:
            if time.monotonic() >= deadline:
                logger.warning(
                    "Google News: hit %ds wall-clock — stopping after %d/%d queries",
                    MAX_TOTAL_WALLCLOCK_SECONDS, run_count, len(queries),
                )
                break

            run_count += 1
            for art in self._safely_query(q):
                if not art.source_url or art.source_url in seen_urls:
                    continue
                seen_urls.add(art.source_url)
                art.country_hint = country_slug
                art.city_hint = city_slug
                articles.append(art)

        elapsed = int(time.monotonic() - start)
        logger.info(
            "Google News: %d unique articles across %d queries in %ds",
            len(articles), run_count, elapsed,
        )
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )

    # ── internals ─────────────────────────────────────────────────────────
    def _safely_query(self, query: str) -> list[ScrapedArticle]:
        try:
            return self._query_articles(query)
        except Exception as exc:
            logger.warning(
                "Google News query %r raised (%s) — skipping",
                query[:40], type(exc).__name__,
            )
            return []

    def _query_articles(self, query: str) -> list[ScrapedArticle]:
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
        body: Optional[str] = None
        for attempt in range(PER_QUERY_MAX_ATTEMPTS):
            try:
                resp = self.client.get(GOOGLE_NEWS_RSS, params=params)
                resp.raise_for_status()
                body = resp.text
                break
            except (
                httpx.ConnectError, httpx.TimeoutException,
                httpx.RemoteProtocolError, httpx.HTTPStatusError, ssl.SSLError,
            ) as exc:
                logger.warning(
                    "Google News query %r attempt %d/%d failed (%s)",
                    query[:40], attempt + 1, PER_QUERY_MAX_ATTEMPTS,
                    type(exc).__name__,
                )
                if attempt + 1 < PER_QUERY_MAX_ATTEMPTS:
                    time.sleep(PER_QUERY_BACKOFF_SECONDS)

        if not body:
            return []
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            logger.warning("Google News query %r returned non-XML: %s", query[:40], exc)
            return []

        out: list[ScrapedArticle] = []
        for item in root.findall(".//item")[:MAX_ITEMS_PER_QUERY]:
            parsed = self._parse_item(item)
            if parsed is not None:
                out.append(parsed)
        return out

    def _parse_item(self, item) -> Optional[ScrapedArticle]:
        try:
            title_full = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_str = (item.findtext("pubDate") or "").strip()
            desc = item.findtext("description") or ""

            if not title_full or not link:
                return None

            title = title_full
            publisher = ""
            if " - " in title_full:
                title, publisher = title_full.rsplit(" - ", 1)
                title = title.strip()
                publisher = publisher.strip()

            pub_date = self._parse_rfc822(pub_str) or date.today()
            publisher_domain = self._publisher_to_domain(publisher)

            return ScrapedArticle(
                headline=title,
                published_date=pub_date,
                source_url=link,
                body_text=None,
                source_name="Google News",
                source_credibility=self._infer_credibility(publisher_domain),
                article_type="news",
                extra_metadata={
                    "publisher": publisher,
                    "publisher_domain": publisher_domain,
                    "snippet": self._strip_html(desc)[:240],
                    "query_via": "google_news_rss",
                },
            )
        except Exception as exc:
            logger.debug("Google News item parse error: %s", exc)
            return None

    @staticmethod
    def _parse_rfc822(s: str) -> Optional[date]:
        if not s:
            return None
        for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S"):
            try:
                return datetime.strptime(s.strip()[:31], fmt).date()
            except ValueError:
                continue
        try:
            return datetime.strptime(s[:25], "%a, %d %b %Y %H:%M:%S").date()
        except ValueError:
            return None

    @staticmethod
    def _publisher_to_domain(publisher: str) -> str:
        if not publisher:
            return ""
        slug = publisher.lower().strip()
        return _PUBLISHER_DOMAIN_ALIASES.get(slug, slug)

    @staticmethod
    def _infer_credibility(domain: str) -> str:
        d = (domain or "").lower()
        if any(h in d for h in HIGH_CREDIBILITY):
            return "tier1"
        return "tier2"

    @staticmethod
    def _strip_html(s: str) -> str:
        if not s:
            return ""
        text = re.sub(r"<[^>]+>", "", s)
        return re.sub(r"\s+", " ", text).strip()
