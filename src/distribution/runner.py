"""Distribution runner — pings Google Indexing API and IndexNow.

Recent BlogPost URLs + newly-generated LandingPage URLs + high-priority
static routes get submitted. Every attempt logged to distribution_logs
with a 23-hour cooldown to prevent re-pings.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from src.config import settings
from src.distribution import google_indexing, indexnow
from src.models import BlogPost, LandingPage, DistributionLog, SessionLocal, init_db

logger = logging.getLogger(__name__)

CHANNEL_GOOGLE_INDEXING = "google_indexing"
CHANNEL_INDEXNOW = "indexnow"

_REPING_COOLDOWN = timedelta(hours=23)

_STATIC_URLS_TO_PING_DAILY = (
    "/",
    "/briefings/",
    "/tools/",
    "/rankings/visa/",
    "/rankings/cost-of-living/",
    "/rankings/internet/",
)


def _site_base() -> str:
    return settings.canonical_site_url.rstrip("/")


def _briefing_url(post: BlogPost) -> str:
    return f"{_site_base()}/briefings/{post.slug}"


def _landing_url(lp: LandingPage) -> str:
    path = lp.canonical_path if lp.canonical_path.startswith("/") else "/" + lp.canonical_path
    return f"{_site_base()}{path}"


def _recent_pinged_urls(db, channel: str, lookback: timedelta) -> set[str]:
    cutoff = datetime.utcnow() - lookback
    rows = (
        db.query(DistributionLog.url)
        .filter(DistributionLog.channel == channel)
        .filter(DistributionLog.success.is_(True))
        .filter(DistributionLog.created_at >= cutoff)
        .all()
    )
    return {r[0] for r in rows}


def _record(db, *, channel: str, url: str, success: bool,
            response_code: int | None, response_snippet: str,
            entity_type: str | None = None, entity_id: int | None = None) -> None:
    db.add(
        DistributionLog(
            channel=channel, url=url,
            entity_type=entity_type, entity_id=entity_id,
            success=success, response_code=response_code,
            response_snippet=response_snippet,
        )
    )


def _gather_candidates(db, already_pinged: set[str]) -> list[tuple[str, str, int | None]]:
    cutoff = datetime.utcnow() - timedelta(days=settings.google_indexing_lookback_days)

    new_posts = (
        db.query(BlogPost)
        .filter(BlogPost.created_at >= cutoff)
        .order_by(BlogPost.created_at.desc())
        .limit(500)
        .all()
    )
    new_landings = (
        db.query(LandingPage)
        .filter(LandingPage.last_generated_at >= cutoff)
        .order_by(LandingPage.last_generated_at.desc())
        .all()
    )

    candidates: list[tuple[str, str, int | None]] = []
    for p in new_posts:
        url = _briefing_url(p)
        if url not in already_pinged:
            candidates.append((url, "blog_post", p.id))
    for lp in new_landings:
        url = _landing_url(lp)
        if url not in already_pinged:
            candidates.append((url, "landing_page", lp.id))
    for path in _STATIC_URLS_TO_PING_DAILY:
        url = _site_base() + path
        if url not in already_pinged:
            candidates.append((url, "static", None))

    return candidates


def run_google_indexing() -> dict:
    if not google_indexing.is_enabled():
        return {"status": "skipped", "reason": "not configured"}
    client = google_indexing.get_client()
    if client is None:
        return {"status": "skipped", "reason": "credentials invalid"}

    init_db()
    db = SessionLocal()
    try:
        already = _recent_pinged_urls(db, CHANNEL_GOOGLE_INDEXING, _REPING_COOLDOWN)
        candidates = _gather_candidates(db, already)
        if len(candidates) > settings.google_indexing_max_per_run:
            candidates = candidates[: settings.google_indexing_max_per_run]
        if not candidates:
            return {"status": "ok", "pinged": 0, "succeeded": 0, "failed": 0, "reason": "nothing new"}

        succeeded = failed = 0
        for url, entity_type, entity_id in candidates:
            result = client.publish_url_updated(url)
            _record(
                db, channel=CHANNEL_GOOGLE_INDEXING, url=url,
                success=result.success, response_code=result.status_code,
                response_snippet=result.response_snippet,
                entity_type=entity_type, entity_id=entity_id,
            )
            if result.success:
                succeeded += 1
            else:
                failed += 1
        db.commit()
        return {"status": "ok", "pinged": len(candidates), "succeeded": succeeded, "failed": failed}
    except Exception as exc:
        logger.exception("google indexing runner failed: %s", exc)
        try: db.rollback()
        except Exception: pass
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


def run_indexnow() -> dict:
    init_db()
    db = SessionLocal()
    try:
        already = _recent_pinged_urls(db, CHANNEL_INDEXNOW, _REPING_COOLDOWN)
        candidates = _gather_candidates(db, already)
        if not candidates:
            return {"status": "ok", "submitted": 0, "reason": "nothing new"}

        urls_only = [c[0] for c in candidates]
        result = indexnow.submit_urls(urls_only)
        for url, entity_type, entity_id in candidates:
            _record(
                db, channel=CHANNEL_INDEXNOW, url=url,
                success=result.success, response_code=result.status_code,
                response_snippet=result.response_snippet,
                entity_type=entity_type, entity_id=entity_id,
            )
        db.commit()
        return {
            "status": "ok" if result.success else "error",
            "submitted": result.submitted,
            "response_code": result.status_code,
        }
    except Exception as exc:
        logger.exception("indexnow runner failed: %s", exc)
        try: db.rollback()
        except Exception: pass
        return {"status": "error", "error": str(exc)}
    finally:
        db.close()


def run_all() -> dict:
    """Run all enabled distribution channels."""
    return {
        CHANNEL_INDEXNOW: run_indexnow(),
        CHANNEL_GOOGLE_INDEXING: run_google_indexing(),
    }
