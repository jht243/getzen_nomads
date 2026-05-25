"""Safety-advisory scraper.

Two real feeds + topical fallback:
  • US State Department travel advisories — RSS
    https://travel.state.gov/_res/rss/TAsTWs.xml
  • UK FCDO travel advice — Atom
    https://www.gov.uk/foreign-travel-advice.atom
  • Google News scoped queries for region-level safety incidents we cover

Country matching: the State Dept feed titles include the country name
("Colombia - Level 3: Reconsider Travel"); we resolve them against the
seeded Country.name list to attach country_id at persist time.
"""
from __future__ import annotations

import logging
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime
from typing import Optional

import httpx

from src.config import settings
from src.scraper.base import BaseScraper, ScrapedArticle, ScrapeResult
from src.scraper.google_news import (
    build_topical_client,
    fetch_google_news_query,
)

logger = logging.getLogger(__name__)

_STATE_DEPT_RSS = "https://travel.state.gov/_res/rss/TAsTWs.xml"
_UK_FCDO_ATOM = "https://www.gov.uk/foreign-travel-advice.atom"

_HARD_DEADLINE_SECONDS = 60


class SafetyAdvisoriesScraper(BaseScraper):
    def __init__(self, *, country_names: Optional[list[str]] = None,
                 country_slugs: Optional[list[str]] = None) -> None:
        super().__init__()
        self.client.close()
        self.client = build_topical_client()
        self._country_names = country_names or []
        self._country_slugs = country_slugs or []
        # Cache: name → slug for hint resolution
        self._name_to_slug: dict[str, str] = {}
        for slug in self._country_slugs:
            # Slugs were generated from names, so simple capitalization is enough
            self._name_to_slug[slug.replace("-", " ").lower()] = slug

    def get_source_id(self) -> str:
        return "safety_advisory"

    def scrape(self, target_date: Optional[date] = None) -> ScrapeResult:
        start = time.monotonic()
        articles: list[ScrapedArticle] = []
        seen: set[str] = set()

        # 1. US State Dept
        try:
            articles.extend(self._scrape_state_dept(seen))
        except Exception as exc:
            logger.warning("safety: state-dept fetch failed (%s)", exc)

        # 2. UK FCDO
        try:
            articles.extend(self._scrape_uk_fcdo(seen))
        except Exception as exc:
            logger.warning("safety: uk-fcdo fetch failed (%s)", exc)

        # 3. Google News topical fallback — covers community-reported scams,
        #    incidents in nomad-popular cities that wouldn't surface in
        #    official advisories.
        if time.monotonic() - start < _HARD_DEADLINE_SECONDS:
            try:
                for country_name in self._country_names[:6]:
                    if time.monotonic() - start >= _HARD_DEADLINE_SECONDS:
                        break
                    query = f'"{country_name}" tourist scam OR robbery OR kidnap OR safety advisory'
                    for art in fetch_google_news_query(self.client, query, max_items=10):
                        if art.source_url in seen:
                            continue
                        seen.add(art.source_url)
                        # Tag with country hint via the country_name → slug map
                        slug = self._name_to_slug.get(country_name.lower())
                        if slug:
                            art.country_hint = slug
                        art.topic_hint = "safety"
                        articles.append(art)
            except Exception as exc:
                logger.warning("safety: topical fallback failed (%s)", exc)

        elapsed = int(time.monotonic() - start)
        logger.info("safety_advisories: %d articles in %ds", len(articles), elapsed)
        return ScrapeResult(
            source=self.get_source_id(),
            success=True,
            articles=articles,
            duration_seconds=elapsed,
        )

    # ── State Dept (RSS) ─────────────────────────────────────────────────
    def _scrape_state_dept(self, seen: set[str]) -> list[ScrapedArticle]:
        resp = self.client.get(_STATE_DEPT_RSS, timeout=20)
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            logger.warning("safety: state-dept XML parse: %s", exc)
            return []

        out: list[ScrapedArticle] = []
        for item in root.findall(".//item"):
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            pub_str = (item.findtext("pubDate") or "").strip()
            desc = (item.findtext("description") or "").strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)

            pub_date = self._parse_rfc822(pub_str) or date.today()
            country_slug = self._extract_country_slug(title)

            out.append(ScrapedArticle(
                headline=title,
                published_date=pub_date,
                source_url=link,
                body_text=re.sub(r"<[^>]+>", "", desc)[:2000],
                source_name="US State Department",
                source_credibility="official",
                article_type="advisory",
                country_hint=country_slug,
                topic_hint="safety",
                extra_metadata={"feed": "state_dept_rss"},
            ))
        return out

    # ── UK FCDO (Atom) ───────────────────────────────────────────────────
    def _scrape_uk_fcdo(self, seen: set[str]) -> list[ScrapedArticle]:
        resp = self.client.get(_UK_FCDO_ATOM, timeout=20)
        resp.raise_for_status()
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as exc:
            logger.warning("safety: uk-fcdo XML parse: %s", exc)
            return []

        ns = {"atom": "http://www.w3.org/2005/Atom"}
        out: list[ScrapedArticle] = []
        for entry in root.findall("atom:entry", ns):
            title = (entry.findtext("atom:title", default="", namespaces=ns) or "").strip()
            link_el = entry.find("atom:link", ns)
            link = link_el.get("href", "").strip() if link_el is not None else ""
            updated = (entry.findtext("atom:updated", default="", namespaces=ns) or "").strip()
            summary = (entry.findtext("atom:summary", default="", namespaces=ns) or "").strip()
            if not title or not link or link in seen:
                continue
            seen.add(link)

            try:
                pub_date = datetime.fromisoformat(updated.replace("Z", "+00:00")).date()
            except ValueError:
                pub_date = date.today()

            country_slug = self._extract_country_slug(title)
            out.append(ScrapedArticle(
                headline=f"FCDO: {title}",
                published_date=pub_date,
                source_url=link,
                body_text=summary[:2000],
                source_name="UK FCDO",
                source_credibility="official",
                article_type="advisory",
                country_hint=country_slug,
                topic_hint="safety",
                extra_metadata={"feed": "uk_fcdo_atom"},
            ))
        return out

    # ── helpers ──────────────────────────────────────────────────────────
    def _extract_country_slug(self, title: str) -> Optional[str]:
        """Match the title against seeded country names."""
        title_lower = (title or "").lower()
        for name in self._country_names:
            if name.lower() in title_lower:
                return self._name_to_slug.get(name.lower())
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
        return None
