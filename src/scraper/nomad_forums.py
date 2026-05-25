"""Nomad-forum scraper — public subreddit RSS feeds.

Pulls /new/.rss for r/digitalnomad, r/expats, r/IWantOut, r/expatfinance.
These are public and don't need OAuth, just a polite User-Agent.

Community-tier credibility — useful for trend-spotting + scam reports
that surface here weeks before mainstream outlets cover them.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Optional

import httpx

from src.config import settings
from src.scraper.base import BaseScraper, ScrapedArticle, ScrapeResult

logger = logging.getLogger(__name__)

_SUBREDDITS = (
    "digitalnomad",
    "expats",
    "IWantOut",
    "expatfinance",
)

_MAX_PER_SUB = 25
_HARD_DEADLINE_SECONDS = 60


class NomadForumsScraper(BaseScraper):
    def __init__(self, *, country_names: Optional[list[str]] = None,
                 country_slugs: Optional[list[str]] = None,
                 city_names_by_country_slug: Optional[dict[str, list[tuple[str, str]]]] = None) -> None:
        super().__init__()
        self.client.close()
        # Reddit needs a unique User-Agent
        self.client = httpx.Client(
            timeout=httpx.Timeout(15.0, connect=8.0),
            follow_redirects=True,
            headers={
                "User-Agent": settings.reddit_user_agent or "getzen/0.1 (https://www.getzen.cash)",
                "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
            },
        )
        self._country_names = country_names or []
        self._country_slugs = country_slugs or []
        self._city_names = city_names_by_country_slug or {}
        self._name_to_slug = {n.lower(): s for s, n in zip(self._country_slugs, self._country_names)}

    def get_source_id(self) -> str:
        return "nomad_forums"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        articles: list[ScrapedArticle] = []
        seen: set[str] = set()
        per_sub_added: dict[str, int] = {}

        for sub in _SUBREDDITS:
            if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                logger.warning("nomad_forums: deadline hit before %s", sub)
                break
            try:
                added = 0
                for art in self._fetch_subreddit(sub):
                    if art.source_url in seen:
                        continue
                    seen.add(art.source_url)
                    articles.append(art)
                    added += 1
                per_sub_added[sub] = added
            except Exception as exc:
                logger.warning("nomad_forums: r/%s failed (%s)", sub, exc)

        elapsed = int(time.monotonic() - start)
        logger.info("nomad_forums: %d posts across %d subs in %ds (%s)",
                    len(articles), len(per_sub_added), elapsed,
                    " ".join(f"r/{k}={v}" for k, v in per_sub_added.items()))
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )

    def _fetch_subreddit(self, sub: str) -> list[ScrapedArticle]:
        url = f"https://www.reddit.com/r/{sub}/new/.rss?limit={_MAX_PER_SUB}"
        resp = self.client.get(url)
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError:
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        out: list[ScrapedArticle] = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "").strip() if link_el is not None else ""
            updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
            content = (entry.findtext("atom:content", default="", namespaces=ns) or "").strip()
            if not title or not link:
                continue
            try:
                pub_date = datetime.fromisoformat(updated.replace("Z", "+00:00")).date()
            except ValueError:
                pub_date = date.today()
            text = re.sub(r"<[^>]+>", "", content)[:3000]

            country_slug, city_slug = self._extract_geo(f"{title} {text}")
            out.append(ScrapedArticle(
                headline=title,
                published_date=pub_date,
                source_url=link,
                body_text=text,
                source_name=f"Reddit r/{sub}",
                source_credibility="community",
                article_type="forum_post",
                country_hint=country_slug,
                city_hint=city_slug,
                extra_metadata={"subreddit": sub},
            ))
        return out

    def _extract_geo(self, text: str) -> tuple[Optional[str], Optional[str]]:
        """Greedy: scan for any seeded city + country name. Cities first
        (more specific). Returns (country_slug, city_slug)."""
        if not text:
            return None, None
        text_lower = text.lower()
        # Cities
        for country_slug, city_pairs in self._city_names.items():
            for city_slug, city_name in city_pairs:
                if city_name.lower() in text_lower:
                    return country_slug, city_slug
        # Countries
        for name, slug in self._name_to_slug.items():
            if name in text_lower:
                return slug, None
        return None, None
