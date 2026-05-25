"""Scraper runner — invokes scrapers and persists results to the DB.

Loads countries/cities from the DB, hands them to scrapers as rotation
context, then upserts ExternalArticleEntry rows + logs to ScrapeLog.
"""
from __future__ import annotations

import logging
import time
from datetime import date
from typing import Optional

from src.models import (
    SessionLocal,
    init_db,
    Country,
    City,
    ExternalArticleEntry,
    ScrapeLog,
    SourceType,
    CredibilityTier,
    ArticleStatus,
)
from src.scraper.base import ScrapedArticle, ScrapeResult
from src.scraper.google_news import GoogleNewsScraper
from src.scraper.safety_advisories import SafetyAdvisoriesScraper
from src.scraper.visa_portals import VisaPortalsScraper
from src.scraper.crypto_regulations import CryptoRegulationsScraper
from src.scraper.nomad_forums import NomadForumsScraper
from src.scraper.government_news import GovernmentNewsScraper

logger = logging.getLogger(__name__)


_CREDIBILITY_MAP = {
    "official": CredibilityTier.OFFICIAL,
    "tier1": CredibilityTier.TIER1,
    "tier2": CredibilityTier.TIER2,
    "community": CredibilityTier.COMMUNITY,
}


def _load_geo() -> tuple[list[str], dict[str, str], dict[tuple[str, str], str], dict[str, int], dict[tuple[str, str], int]]:
    """Returns (country_slugs, country_name_by_slug, city_name_by_country_city,
    country_id_by_slug, city_id_by_country_city)."""
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).all()
        cities = session.query(City).all()
        country_slugs = [c.slug for c in countries]
        country_name = {c.slug: c.name for c in countries}
        country_id = {c.slug: c.id for c in countries}
        slug_by_id = {c.id: c.slug for c in countries}
        city_name = {(slug_by_id[ci.country_id], ci.slug): ci.name for ci in cities if ci.country_id in slug_by_id}
        city_id = {(slug_by_id[ci.country_id], ci.slug): ci.id for ci in cities if ci.country_id in slug_by_id}
        return country_slugs, country_name, city_name, country_id, city_id
    finally:
        session.close()


def _resolve_source_type(source_id: str) -> SourceType:
    return {
        "google_news": SourceType.GOOGLE_NEWS,
        "visa_portal": SourceType.VISA_PORTAL,
        "safety_advisory": SourceType.SAFETY_ADVISORY,
        "speedtest": SourceType.SPEEDTEST,
        "cost_of_living": SourceType.COST_OF_LIVING,
        "crypto_regulations": SourceType.CRYPTO_REGULATIONS,
        "housing": SourceType.HOUSING,
        "coworking": SourceType.COWORKING,
        "nomad_forums": SourceType.NOMAD_FORUMS,
        "government_news": SourceType.GOVERNMENT_NEWS,
        "healthcare": SourceType.HEALTHCARE,
    }.get(source_id, SourceType.GOOGLE_NEWS)


def _persist(result: ScrapeResult, *, country_id_by_slug: dict[str, int], city_id_by_pair: dict[tuple[str, str], int]) -> int:
    """Upsert articles. Returns count of NEW rows inserted."""
    session = SessionLocal()
    inserted = 0
    src_enum = _resolve_source_type(result.source)

    try:
        for art in result.articles:
            # Dedup on (source, source_url)
            existing = (
                session.query(ExternalArticleEntry)
                .filter_by(source=src_enum, source_url=art.source_url)
                .one_or_none()
            )
            if existing is not None:
                continue

            country_id = country_id_by_slug.get(art.country_hint) if art.country_hint else None
            city_id = None
            if art.country_hint and art.city_hint:
                city_id = city_id_by_pair.get((art.country_hint, art.city_hint))

            row = ExternalArticleEntry(
                source=src_enum,
                source_url=art.source_url,
                source_name=art.source_name or "",
                credibility=_CREDIBILITY_MAP.get(art.source_credibility, CredibilityTier.TIER2),
                headline=art.headline,
                published_date=art.published_date,
                body_text=art.body_text,
                article_type=art.article_type,
                country_id=country_id,
                city_id=city_id,
                topic_id=None,    # analyzer assigns
                relevance_score=None,
                extra_metadata=art.extra_metadata or {},
                status=ArticleStatus.SCRAPED,
            )
            session.add(row)
            inserted += 1

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    return inserted


def _log_run(result: ScrapeResult, *, target_date: date) -> None:
    session = SessionLocal()
    try:
        log = ScrapeLog(
            source=_resolve_source_type(result.source),
            scrape_date=target_date,
            success=result.success,
            entries_found=len(result.articles),
            error_message=result.error,
            duration_seconds=result.duration_seconds,
        )
        session.add(log)
        session.commit()
    except Exception:
        session.rollback()
    finally:
        session.close()


def run_scrapers(target_date: Optional[date] = None) -> dict:
    """Run all configured scrapers. Returns per-source summary."""
    init_db()
    target_date = target_date or date.today()
    country_slugs, country_name, city_name, country_id, city_id = _load_geo()

    summary: dict[str, dict] = {}

    # Index country names + city groupings for the topic-scoped scrapers.
    country_names_ordered = [country_name[s] for s in country_slugs]
    city_names_by_country_slug: dict[str, list[tuple[str, str]]] = {}
    for (cslug, city_slug), city_full in city_name.items():
        city_names_by_country_slug.setdefault(cslug, []).append((city_slug, city_full))

    scrapers = [
        # P0 daily — country-rotated general news
        GoogleNewsScraper(
            country_slugs=country_slugs,
            country_name_by_slug=country_name,
            city_name_by_slug=city_name,
        ),
        # P0 daily — official advisories + topical safety news
        SafetyAdvisoriesScraper(
            country_names=country_names_ordered,
            country_slugs=country_slugs,
        ),
        # P0 daily — community sentiment / scam reports
        NomadForumsScraper(
            country_names=country_names_ordered,
            country_slugs=country_slugs,
            city_names_by_country_slug=city_names_by_country_slug,
        ),
        # P0 weekly cadence in production — visa policy changes
        VisaPortalsScraper(
            country_names=country_names_ordered,
            country_slugs=country_slugs,
        ),
        # P1 daily — crypto regulation
        CryptoRegulationsScraper(
            country_names=country_names_ordered,
            country_slugs=country_slugs,
        ),
        # P1 daily — broader government program coverage
        GovernmentNewsScraper(
            country_names=country_names_ordered,
            country_slugs=country_slugs,
        ),
    ]

    for scraper in scrapers:
        sid = scraper.get_source_id()
        logger.info("Running scraper: %s", sid)
        try:
            with scraper:
                result = scraper.scrape(target_date=target_date)
        except Exception as exc:
            logger.exception("Scraper %s crashed: %s", sid, exc)
            result = ScrapeResult(source=sid, success=False, error=str(exc))

        inserted = _persist(
            result,
            country_id_by_slug=country_id,
            city_id_by_pair=city_id,
        ) if result.success else 0

        _log_run(result, target_date=target_date)
        summary[sid] = {
            "success": result.success,
            "found": len(result.articles),
            "inserted": inserted,
            "duration_seconds": result.duration_seconds,
            "error": result.error,
        }
        logger.info(
            "%s: found=%d inserted=%d duration=%ds success=%s",
            sid, len(result.articles), inserted, result.duration_seconds, result.success,
        )

    return summary


if __name__ == "__main__":
    import logging, sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    out = run_scrapers()
    print(out)
