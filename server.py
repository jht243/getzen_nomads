"""Get ZEN Flask app.

Routes: homepage, briefings index/detail, country/city/topic landing pages,
tool placeholders, sitemap.xml, robots.txt, health, subscribe/feedback stubs.
"""
from __future__ import annotations

import gzip
import io
import json
import logging
from datetime import date, datetime, timezone
from functools import lru_cache

from flask import Flask, Response, abort, redirect, render_template, request, jsonify
from sqlalchemy import select, func as sa_func

from src.config import settings


def _current_year() -> int:
    return datetime.utcnow().year


def _seo_pad_title(s: str, *, target_min: int = 50, target_max: int = 60) -> str:
    """Right-pad a short title with a brand suffix to land in the SERP window.

    No-op when the title already contains the brand — avoids the
    "— Get ZEN — Get ZEN" double-suffix.
    """
    s = (s or "").strip()
    if len(s) >= target_min:
        return s
    has_brand = "Get ZEN" in s
    suffix_candidates = (
        [""] if has_brand else
        [" — Digital Nomad Intelligence", " — Get ZEN", ""]
    )
    if has_brand:
        suffix_candidates = [
            " · 2026 Update",
            " · Digital Edition",
            " · Practical Guide",
            "",
        ]
    for suffix in suffix_candidates:
        candidate = (s + suffix).strip()
        if target_min <= len(candidate) <= target_max:
            return candidate
    # Final fallback: longest candidate that doesn't exceed target_max
    for suffix in suffix_candidates:
        candidate = (s + suffix).strip()
        if len(candidate) <= target_max:
            return candidate
    return s[:target_max]


def _hero_subtitle(text: str, *, max_chars: int = 130) -> str:
    """Trim a long summary to ≤2 hero lines at the sentence/clause boundary.

    Hero subtitle CSS is `-webkit-line-clamp: 2` at 640px width / 17px italic
    serif — that fits ~120–140 chars total. We prefer to cut at a sentence
    end so the truncation reads cleanly instead of mid-word ellipsis.
    """
    s = " ".join((text or "").split())
    if len(s) <= max_chars:
        return s
    # Try sentence boundary first (period, em-dash, semicolon, colon)
    for sep in (". ", "? ", "! ", "; ", " — ", ": "):
        idx = s[:max_chars].rfind(sep)
        if idx > max_chars // 2:
            return s[: idx + 1].rstrip(" —;:")
    # Fall back to word boundary
    return s[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


def _seo_pad_description(s: str, *, target_min: int = 120, target_max: int = 160) -> str:
    s = (s or "").strip()
    if target_min <= len(s) <= target_max:
        return s
    if len(s) < target_min:
        suffix = " Updated daily with practical visa, cost, internet, and safety intel for digital nomads."
        candidate = (s + (" " if s else "") + suffix.lstrip()).strip()
        if len(candidate) > target_max:
            # Truncate suffix to fit
            room = target_max - len(s) - 1
            if room > 0:
                candidate = (s + " " + suffix[: room].rstrip(" ,.;:")).strip()
        return candidate[:target_max]
    return s[:target_max]
from src.models import (
    SessionLocal,
    init_db,
    Country,
    City,
    Topic,
    BlogPost,
    LandingPage,
)
from src.page_renderer import (
    register_jinja,
    seo_for_page,
    absolute_url,
    jsonld_news_article,
    jsonld_landing,
    jsonld_breadcrumbs,
    jsonld_faq,
    jsonld_website,
    jsonld_place_country,
    jsonld_place_city,
    jsonld_article_fallback,
    jsonld_collection_page,
    render_jsonld,
)
from src.seo.cluster_topology import build_cluster_ctx, RANKABLE_TOPIC_SLUGS
from src.og_image import (
    render_briefing_card,
    render_landing_card,
    render_default_card,
)

logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

app = Flask(__name__, template_folder="templates", static_folder="static")
register_jinja(app)
init_db()


# ── Gzip middleware ───────────────────────────────────────────────────────
_GZIP_TYPES = {
    "text/html",
    "text/plain",
    "text/css",
    "text/xml",
    "application/json",
    "application/xml",
    "application/ld+json",
    "image/svg+xml",
}
_GZIP_MIN = 500


@app.after_request
def _gzip_response(response: Response) -> Response:
    ae = request.headers.get("Accept-Encoding", "")
    if "gzip" not in ae:
        return response
    if response.direct_passthrough or response.status_code < 200 or response.status_code >= 300:
        return response
    ctype = (response.content_type or "").split(";")[0].strip()
    if ctype not in _GZIP_TYPES:
        return response
    data = response.get_data()
    if len(data) < _GZIP_MIN:
        return response
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        gz.write(data)
    response.set_data(buf.getvalue())
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = str(len(response.get_data()))
    response.headers["Vary"] = "Accept-Encoding"
    return response


# ── Tool registry ─────────────────────────────────────────────────────────
TOOLS: dict[str, dict] = {
    "visa-finder": {
        "slug": "visa-finder",
        "title": "Digital Nomad Visa Finder",
        "subtitle": "Find the right visa for your next destination.",
        "description": (
            "Filter every digital-nomad and long-stay visa across our "
            "destinations by income requirement, duration, cost, and renewal "
            "mechanics — with sources and verification dates."
        ),
    },
    "cost-calculator": {
        "slug": "cost-calculator",
        "title": "Cost of Living Calculator",
        "subtitle": "Compare monthly costs across destinations.",
        "description": (
            "Build a realistic monthly budget by city — rent, groceries, "
            "transport, dining, coworking, healthcare — and compare side by "
            "side. Sourced from on-the-ground submissions and verified "
            "datasets."
        ),
    },
    "internet-tracker": {
        "slug": "internet-tracker",
        "title": "Internet Speed Tracker",
        "subtitle": "Real-time internet reliability data for remote workers.",
        "description": (
            "Live download, upload, and latency measurements per city — "
            "tracked over time so you can spot outage patterns and seasonal "
            "drops before you commit to a long stay."
        ),
    },
    "safety-dashboard": {
        "slug": "safety-dashboard",
        "title": "Safety Score Dashboard",
        "subtitle": "Safety intelligence for digital nomads.",
        "description": (
            "Neighborhood-level safety scoring combining government advisories, "
            "community-reported scams, and recent incidents — refreshed daily."
        ),
    },
}


# ── Small caches (90s nav pages, 600s briefings) ──────────────────────────
_nav_cache: dict[str, tuple[float, str]] = {}
_NAV_TTL = 90


def _cached_render(key: str, render_fn, ttl: int = _NAV_TTL) -> Response:
    import time
    now = time.time()
    hit = _nav_cache.get(key)
    if hit and now - hit[0] < ttl:
        return Response(hit[1], mimetype="text/html")
    html = render_fn()
    _nav_cache[key] = (now, html)
    return Response(html, mimetype="text/html")


# ── Homepage ──────────────────────────────────────────────────────────────
@app.route("/")
def homepage():
    def _render() -> str:
        session = SessionLocal()
        try:
            posts = (
                session.query(BlogPost)
                .order_by(BlogPost.published_date.desc())
                .limit(6)
                .all()
            )
            latest = []
            for p in posts:
                country_name = None
                if p.country_id:
                    c = session.get(Country, p.country_id)
                    country_name = c.name if c else None
                latest.append(
                    type(
                        "B",
                        (),
                        {
                            "slug": p.slug,
                            "title": p.title,
                            "subtitle": p.subtitle,
                            "summary": p.summary,
                            "country_name": country_name,
                        },
                    )()
                )
            countries = session.query(Country).order_by(Country.name).all()
            # Build per-country city counts for the destination cards.
            country_city_counts = dict(
                session.query(City.country_id, sa_func.count(City.id))
                .group_by(City.country_id)
                .all()
            )
            country_ctxs = []
            for c in countries:
                country_ctxs.append(type("CC", (), {
                    "slug": c.slug,
                    "name": c.name,
                    "region": c.region or "",
                    "summary": c.summary or "",
                    "nomad_visa_available": bool(c.nomad_visa_available),
                    "city_count": country_city_counts.get(c.id, 0),
                })())
            total_countries = len(countries)
            total_cities = sum(country_city_counts.values())
            total_briefings = session.query(BlogPost).count()
            total_topics = session.query(Topic).count()

            # Rankable topic taxonomy for the homepage rankings grid.
            topics_all = session.query(Topic).order_by(Topic.display_order).all()
            topic_by_slug = {t.slug: t for t in topics_all}
            ranking_topics = [
                topic_by_slug[s] for s in RANKABLE_TOPIC_SLUGS if s in topic_by_slug
            ]

            # Featured leaderboards — top 5 for the 4 most-searched rankings.
            # Compute against the same logic as /rankings/{topic}/ for parity.
            featured = []
            for feat_slug, feat_name in (
                ("visa", "digital nomad visas"),
                ("cost-of-living", "cheapest destinations"),
                ("internet", "fastest internet"),
                ("safety", "safest destinations"),
            ):
                rows = _build_ranking_rows(feat_slug, countries)[:5]
                featured.append({
                    "topic_slug": feat_slug,
                    "topic_name": feat_name,
                    "rows": rows,
                })
        finally:
            session.close()

        seo = seo_for_page(
            title=_seo_pad_title(
                f"Get ZEN ({_current_year()}): Digital Nomad Intelligence"
            ),
            description=_seo_pad_description(
                "Practical visa, cost, internet, safety, and crypto intelligence "
                "for digital nomads heading to emerging destinations. "
                f"Updated daily for {_current_year()}."
            ),
            path="/",
            og_type="website",
        )
        return render_template(
            "homepage.html.j2",
            seo=seo,
            jsonld=render_jsonld(jsonld_website()),
            latest_briefings=latest,
            featured_countries=country_ctxs,
            ranking_topics=ranking_topics,
            featured_rankings=featured,
            stats={
                "destinations": total_countries,
                "cities": total_cities,
                "topics": total_topics,
                "briefings": total_briefings,
            },
        )

    return _cached_render("home", _render)


# ── Briefings ─────────────────────────────────────────────────────────────
@app.route("/briefings/")
def briefings_index():
    page_n = max(1, int(request.args.get("page", 1)))
    per_page = 20

    def _render() -> str:
        session = SessionLocal()
        try:
            base_q = session.query(BlogPost).order_by(BlogPost.published_date.desc())
            total = base_q.count()
            rows = base_q.offset((page_n - 1) * per_page).limit(per_page).all()
            posts = []
            for p in rows:
                country_name = topic_name = None
                if p.country_id:
                    c = session.get(Country, p.country_id)
                    country_name = c.name if c else None
                if p.topic_id:
                    t = session.get(Topic, p.topic_id)
                    topic_name = t.name if t else None
                posts.append(
                    type(
                        "P",
                        (),
                        {
                            "slug": p.slug,
                            "title": p.title,
                            "subtitle": p.subtitle,
                            "summary": p.summary,
                            "published_date": p.published_date,
                            "country_name": country_name,
                            "topic_name": topic_name,
                        },
                    )()
                )
        finally:
            session.close()

        pages = max(1, (total + per_page - 1) // per_page)
        pagination = {
            "page": page_n,
            "pages": pages,
            "prev": f"/briefings/?page={page_n - 1}" if page_n > 1 else None,
            "next": f"/briefings/?page={page_n + 1}" if page_n < pages else None,
        }
        seo = seo_for_page(
            title=_seo_pad_title(
                f"Daily Digital Nomad Briefings ({_current_year()}) — Get ZEN"
            ),
            description=(
                "Daily intelligence on visa changes, costs, safety alerts, "
                "internet quality, and crypto regulation across emerging "
                "and emerging nomad destinations. Updated twice daily."
            ),
            path="/briefings/",
            og_type="website",
        )
        canonical = absolute_url("/briefings/")
        nodes = [
            jsonld_collection_page(
                name=f"Get ZEN Daily Briefings — {_current_year()}",
                canonical=canonical,
                description="Daily digital nomad intelligence — visa, cost, safety, internet, crypto.",
            ),
            jsonld_breadcrumbs([("Briefings", "/briefings/")]),
        ]
        return render_template(
            "briefing_index.html.j2",
            seo=seo,
            jsonld=render_jsonld(*nodes),
            posts=posts,
            pagination=pagination,
        )

    return _cached_render(f"briefings:{page_n}", _render)


@app.route("/briefings/<slug>")
def briefing_post(slug: str):
    session = SessionLocal()
    try:
        post = session.query(BlogPost).filter_by(slug=slug).one_or_none()
        if not post:
            abort(404)

        country = session.get(Country, post.country_id) if post.country_id else None
        topic = session.get(Topic, post.topic_id) if post.topic_id else None

        related_rows = (
            session.query(BlogPost)
            .filter(BlogPost.id != post.id)
            .filter(BlogPost.country_id == post.country_id if post.country_id else True)
            .order_by(BlogPost.published_date.desc())
            .limit(3)
            .all()
        )
        related = [
            type("R", (), {"slug": r.slug, "title": r.title, "summary": r.summary})()
            for r in related_rows
        ]

        post_ctx = type(
            "P",
            (),
            {
                "title": post.title,
                "subtitle": post.subtitle,
                "summary": post.summary,
                "body_html": post.body_html,
                "published_date": post.published_date,
                "reading_minutes": post.reading_minutes,
                "canonical_source_url": post.canonical_source_url,
                "country_name": country.name if country else None,
                "topic_name": topic.name if topic else None,
                "takeaways": post.takeaways_json or [],
                "word_count": post.word_count,
                "keywords_json": post.keywords_json or [],
                "updated_at": post.updated_at,
                "created_at": post.created_at,
            },
        )()

        path = f"/briefings/{slug}"
        canonical = absolute_url(path)
        og_image = absolute_url(f"/og/briefing/{slug}.png")

        crumbs = [("Briefings", "/briefings/"), (post.title, path)]
        seo = seo_for_page(
            title=post.title,
            description=post.summary or post.subtitle or "",
            path=path,
            og_image=og_image,
            og_type="article",
            keywords=post.keywords_json or [],
            news_keywords=post.keywords_json or [],
            published_iso=_iso(post.published_date),
            modified_iso=_iso(post.updated_at or post.created_at),
            section=topic.name if topic else None,
            article_tags=post.keywords_json or [],
        )
        nodes = [
            jsonld_news_article(post, canonical, og_image),
            jsonld_breadcrumbs(crumbs),
        ]
        return render_template(
            "briefing_post.html.j2",
            seo=seo,
            jsonld=render_jsonld(*nodes),
            post=post_ctx,
            related=related,
        )
    finally:
        session.close()


# ── Country / City / Topic landing pages ──────────────────────────────────
@app.route("/<country_slug>/")
def country_hub(country_slug: str):
    # Reserved top-level paths handled by their own routes — guard so they
    # don't get swallowed by this catch-all country slug pattern.
    if country_slug in _RESERVED_TOP_LEVEL:
        abort(404)

    def _render() -> str:
        session = SessionLocal()
        try:
            country = session.query(Country).filter_by(slug=country_slug).one_or_none()
            if not country:
                return "__404__"
            cities = (
                session.query(City)
                .filter_by(country_id=country.id)
                .order_by(City.name)
                .all()
            )
            page_row = (
                session.query(LandingPage)
                .filter_by(page_key=f"country:{country.slug}")
                .one_or_none()
            )
            briefings = (
                session.query(BlogPost)
                .filter_by(country_id=country.id)
                .order_by(BlogPost.published_date.desc())
                .limit(3)
                .all()
            )

            page_ctx = _landing_to_ctx(page_row) if page_row else type("P", (), {"title": None, "subtitle": None, "body_html": None, "faq": None})()
            # Truncate hero subtitle at sentence boundary so CSS line-clamp
            # doesn't cut mid-word.
            page_ctx.hero_subtitle = _hero_subtitle(
                page_ctx.subtitle if page_ctx.subtitle else (country.summary or "")
            )
            briefing_ctxs = [
                type("B", (), {"slug": b.slug, "title": b.title, "summary": b.summary})()
                for b in briefings
            ]
        finally:
            session.close()

        path = f"/{country.slug}/"
        year = _current_year()
        # Fallback title: "{Country} for Digital Nomads ({year}): Visa, Cost & Safety"
        fallback_title = f"{country.name} for Digital Nomads ({year}): Visa, Cost & Safety"
        title = _seo_pad_title(page_row.title if page_row else fallback_title)
        fallback_desc = (
            f"Practical {country.name} digital nomad guide — visa rules, "
            f"monthly cost ranges, internet speeds, safety patterns and where "
            f"to base yourself. Updated for {year}."
        )
        description = _seo_pad_description(page_row.summary if page_row else (country.summary or fallback_desc))

        seo = seo_for_page(
            title=title,
            description=description,
            path=path,
            og_image=absolute_url(f"/og/{country.slug}/og.png"),
            og_type="website",
        )
        nodes = [
            jsonld_landing(page_row, absolute_url(path)) if page_row
                else jsonld_place_country(
                    name=country.name,
                    canonical=absolute_url(path),
                    description=description,
                    iso_code=country.iso_code,
                ),
            jsonld_breadcrumbs([(country.name, path)]),
            jsonld_faq(page_row.faq_json) if (page_row and page_row.faq_json) else None,
        ]
        return render_template(
            "country_hub.html.j2",
            seo=seo,
            jsonld=render_jsonld(*nodes),
            country=country,
            cities=cities,
            page=page_ctx,
            briefings=briefing_ctxs,
            cluster_ctx=build_cluster_ctx(path),
            stats={
                "cities": len(cities),
                "topics": 11,
                "briefings": len(briefing_ctxs),
            },
        )

    rendered = _cached_render(f"country:{country_slug}", _render)
    if rendered.get_data(as_text=True) == "__404__":
        abort(404)
    return rendered


@app.route("/<country_slug>/<city_or_topic_slug>/")
def city_or_country_topic(country_slug: str, city_or_topic_slug: str):
    """Dispatcher: /{country}/{slug}/ may be either a city guide or a
    country-topic guide. Topic slugs are a fixed closed set, so we try
    Topic first, falling through to City. Reserved top-level paths
    short-circuit to 404."""
    if country_slug in _RESERVED_TOP_LEVEL:
        abort(404)
    session = SessionLocal()
    try:
        topic = session.query(Topic).filter_by(slug=city_or_topic_slug).one_or_none()
    finally:
        session.close()
    if topic is not None:
        return country_topic_guide(country_slug, city_or_topic_slug)
    return city_guide(country_slug, city_or_topic_slug)


def country_topic_guide(country_slug: str, topic_slug: str):
    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        if not country:
            abort(404)
        topic = session.query(Topic).filter_by(slug=topic_slug).one_or_none()
        if not topic:
            abort(404)
        cities = (
            session.query(City)
            .filter_by(country_id=country.id)
            .order_by(City.name)
            .all()
        )
        page_row = (
            session.query(LandingPage)
            .filter_by(page_key=f"country-topic:{country.slug}:{topic.slug}")
            .one_or_none()
        )
        page_ctx = _landing_to_ctx(page_row) if page_row else type("P", (), {
            "title": None, "subtitle": None, "body_html": None, "faq": None,
        })()
        fallback_sub = (
            f"{topic.name} intelligence for digital nomads in {country.name} — "
            f"city deep-dives, current data, and the practical mechanics."
        )
        page_ctx.hero_subtitle = _hero_subtitle(
            page_ctx.subtitle if page_ctx.subtitle else fallback_sub
        )
    finally:
        session.close()

    path = f"/{country.slug}/{topic.slug}/"
    year = _current_year()
    fallback_title = f"{topic.name} in {country.name} ({year}): A Digital Nomad's Guide"
    title = _seo_pad_title(page_row.title if page_row else fallback_title)
    description = _seo_pad_description(
        page_row.summary if page_row
        else f"{topic.name} guide for digital nomads in {country.name} — current data, city deep-dives, and practical mechanics for {year}."
    )
    seo = seo_for_page(title=title, description=description, path=path, og_type="article")
    nodes = [
        jsonld_landing(page_row, absolute_url(path), page_type="Article") if page_row
            else jsonld_article_fallback(
                headline=fallback_title,
                canonical=absolute_url(path),
                description=description,
            ),
        jsonld_breadcrumbs([
            ("Rankings", "/rankings/"),
            (f"{topic.name} Rankings", f"/rankings/{topic.slug}/"),
            (country.name, f"/{country.slug}/"),
            (topic.name, path),
        ]),
        jsonld_faq(page_row.faq_json) if (page_row and page_row.faq_json) else None,
    ]
    return render_template(
        "country_topic_guide.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        country=country,
        topic=topic,
        page=page_ctx,
        cities=cities,
        current_year=year,
        cluster_ctx=build_cluster_ctx(path),
    )


def city_guide(country_slug: str, city_slug: str):
    if country_slug in _RESERVED_TOP_LEVEL:
        abort(404)

    def _render() -> str:
        session = SessionLocal()
        try:
            country = session.query(Country).filter_by(slug=country_slug).one_or_none()
            if not country:
                return "__404__"
            city = (
                session.query(City)
                .filter_by(country_id=country.id, slug=city_slug)
                .one_or_none()
            )
            if not city:
                return "__404__"
            topics = session.query(Topic).order_by(Topic.display_order).all()
            page_row = (
                session.query(LandingPage)
                .filter_by(page_key=f"city:{country.slug}:{city.slug}")
                .one_or_none()
            )
            page_ctx = _landing_to_ctx(page_row) if page_row else type("P", (), {"title": None, "subtitle": None, "body_html": None, "faq": None})()
            page_ctx.hero_subtitle = _hero_subtitle(
                page_ctx.subtitle if page_ctx.subtitle
                else (city.summary
                      or f"Practical {city.name} nomad intel — visa, cost, internet, and safety for remote workers in {country.name}.")
            )
        finally:
            session.close()

        path = f"/{country.slug}/{city.slug}/"
        year = _current_year()
        fallback_title = f"{city.name}, {country.name} Digital Nomad Guide ({year})"
        title = _seo_pad_title(page_row.title if page_row else fallback_title)
        fallback_desc = (
            f"On-the-ground {city.name} digital nomad guide — monthly costs, "
            f"internet speeds, coworking, safety by neighborhood, expat community. "
            f"Updated for {year}."
        )
        description = _seo_pad_description(page_row.summary if page_row else (city.summary or fallback_desc))
        seo = seo_for_page(title=title, description=description, path=path, og_type="website")
        nodes = [
            jsonld_landing(page_row, absolute_url(path)) if page_row
                else jsonld_place_city(
                    name=city.name,
                    canonical=absolute_url(path),
                    description=description,
                    country_name=country.name,
                    lat=city.lat,
                    lon=city.lon,
                ),
            jsonld_breadcrumbs([(country.name, f"/{country.slug}/"), (city.name, path)]),
            jsonld_faq(page_row.faq_json) if (page_row and page_row.faq_json) else None,
        ]
        return render_template(
            "city_guide.html.j2",
            seo=seo,
            jsonld=render_jsonld(*nodes),
            country=country,
            city=city,
            page=page_ctx,
            topics=topics,
            cluster_ctx=build_cluster_ctx(path),
        )

    rendered = _cached_render(f"city:{country_slug}:{city_slug}", _render)
    if rendered.get_data(as_text=True) == "__404__":
        abort(404)
    return rendered


@app.route("/<country_slug>/<city_slug>/<topic_slug>/")
def topic_page(country_slug: str, city_slug: str, topic_slug: str):
    if country_slug in _RESERVED_TOP_LEVEL:
        abort(404)
    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        if not country:
            abort(404)
        city = (
            session.query(City)
            .filter_by(country_id=country.id, slug=city_slug)
            .one_or_none()
        )
        if not city:
            abort(404)
        topic = session.query(Topic).filter_by(slug=topic_slug).one_or_none()
        if not topic:
            abort(404)
        page_row = (
            session.query(LandingPage)
            .filter_by(page_key=f"topic:{country.slug}:{city.slug}:{topic.slug}")
            .one_or_none()
        )
        page_ctx = _landing_to_ctx(page_row) if page_row else type("P", (), {"title": None, "subtitle": None, "body_html": None, "faq": None})()
        page_ctx.hero_subtitle = _hero_subtitle(
            page_ctx.subtitle if page_ctx.subtitle
            else (topic.description
                  or f"{topic.name} intelligence for digital nomads in {city.name}, {country.name}.")
        )
    finally:
        session.close()

    path = f"/{country.slug}/{city.slug}/{topic.slug}/"
    year = _current_year()
    fallback_title = f"{topic.name} in {city.name}, {country.name} ({year}): A Nomad's Guide"
    title = _seo_pad_title(page_row.title if page_row else fallback_title)
    fallback_desc = (
        f"{topic.name} intelligence for digital nomads in {city.name}, "
        f"{country.name} — current prices, providers, neighborhoods and what "
        f"to verify before you move. Updated for {year}."
    )
    description = _seo_pad_description(page_row.summary if page_row else (topic.description or fallback_desc))
    seo = seo_for_page(title=title, description=description, path=path, og_type="article")
    nodes = [
        jsonld_landing(page_row, absolute_url(path), page_type="Article") if page_row
            else jsonld_article_fallback(
                headline=fallback_title,
                canonical=absolute_url(path),
                description=description,
            ),
        jsonld_breadcrumbs([
            (country.name, f"/{country.slug}/"),
            (city.name, f"/{country.slug}/{city.slug}/"),
            (topic.name, path),
        ]),
        jsonld_faq(page_row.faq_json) if (page_row and page_row.faq_json) else None,
    ]
    return render_template(
        "topic_page.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        country=country,
        city=city,
        topic=topic,
        page=page_ctx,
        cluster_ctx=build_cluster_ctx(path),
    )


# ── Tools ─────────────────────────────────────────────────────────────────
@app.route("/tools/")
def tools_index():
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).limit(6).all()
    finally:
        session.close()
    seo = seo_for_page(
        title=_seo_pad_title(f"Digital Nomad Tools ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(
            "Interactive tools for digital nomads — visa finder, cost calculator, "
            "internet speed tracker, and safety dashboard. Built on Get ZEN's "
            "continuously-updated digital nomad dataset."
        ),
        path="/tools/",
        og_type="website",
    )
    canonical = absolute_url("/tools/")
    nodes = [
        jsonld_collection_page(
            name="Get ZEN Tools",
            canonical=canonical,
            description="Interactive tools for digital nomads — visa finder, cost calculator, internet tracker, safety dashboard.",
        ),
        jsonld_breadcrumbs([("Tools", "/tools/")]),
    ]
    return render_template(
        "tool_placeholder.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        tool={
            "title": "Get ZEN Tools",
            "subtitle": "Interactive tools for digital nomads.",
            "description": (
                "Four tools are in development — visa finder, cost calculator, "
                "internet tracker, safety dashboard. Each draws from our "
                "continuously-updated dataset."
            ),
        },
        related_countries=countries,
    )


@app.route("/tools/<tool_slug>/")
def tool_detail(tool_slug: str):
    tool = TOOLS.get(tool_slug)
    if not tool:
        abort(404)
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).limit(6).all()
    finally:
        session.close()
    path = f"/tools/{tool_slug}/"
    seo = seo_for_page(
        title=_seo_pad_title(f"{tool['title']} ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(tool.get("description") or tool["subtitle"]),
        path=path,
        og_type="website",
    )
    nodes = [
        jsonld_collection_page(
            name=tool["title"],
            canonical=absolute_url(path),
            description=tool.get("description") or tool["subtitle"],
        ),
        jsonld_breadcrumbs([("Tools", "/tools/"), (tool["title"], path)]),
    ]
    return render_template(
        "tool_placeholder.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        tool=tool,
        related_countries=countries,
    )


# ── Legacy programmatic URLs — permanent redirects to /rankings/{topic}/ ─
@app.route("/visa-database/")
def visa_database_legacy():
    return redirect("/rankings/visa/", code=301)


@app.route("/cost-of-living-rankings/")
def cost_rankings_legacy():
    return redirect("/rankings/cost-of-living/", code=301)


@app.route("/internet-speeds/")
def internet_speeds_legacy():
    return redirect("/rankings/internet/", code=301)


# ── Rankings hub + per-topic ranking ─────────────────────────────────────
_RANKING_META: dict[str, dict] = {
    "visa": {
        "title": "Digital Nomad Visa Rankings",
        "lead": "Every digital-nomad and long-stay visa across our destinations, ranked by accessibility, income requirement, and renewal mechanics.",
        "extra_columns": ["Visa", "Income req"],
        "methodology": "Rankings combine availability of a dedicated nomad visa, monthly income requirement, application cost, and renewal flexibility. Sourced from official immigration portals.",
    },
    "cost-of-living": {
        "title": "Cost of Living Rankings for Digital Nomads",
        "lead": "Monthly nomad budgets by city — destinations ranked by total cost, rent, dining, and coworking.",
        "extra_columns": ["$/mo (typ.)"],
        "methodology": "Monthly budget figures combine rent, groceries, dining, transport, and coworking. Sourced from Numbeo + on-the-ground submissions.",
    },
    "internet": {
        "title": "Internet Speeds by City for Remote Workers",
        "lead": "Download, upload, and latency measurements per city — tracked over time so seasonal drops and outage patterns surface.",
        "extra_columns": ["Avg Mbps"],
        "methodology": "Median Ookla Speedtest measurements over the rolling 90-day window, plus on-the-ground latency from nomad-frequented coworking spaces.",
    },
    "safety": {
        "title": "Safety Rankings for Digital Nomads",
        "lead": "Neighborhood-level safety scoring combining government advisories, crime data, and community-reported incidents.",
        "extra_columns": ["Advisory"],
        "methodology": "Composite of US State Department + UK FCDO advisory levels, crime indices, and Get ZEN's nomad-forum incident scraper.",
    },
    "crypto": {
        "title": "Crypto-Friendly Rankings for Digital Nomads",
        "lead": "Where Bitcoin, stablecoin, and on/off-ramps work for nomads — destinations ranked by banking access and regulatory clarity.",
        "extra_columns": ["Crypto"],
        "methodology": "Combines local crypto regulation, banking access for crypto-funded nomads, and stablecoin acceptance.",
    },
    "banking": {
        "title": "Banking Access Rankings for Digital Nomads",
        "lead": "Account-opening difficulty, multi-currency support, and parallel-rate access for remote workers.",
        "extra_columns": ["Banking"],
        "methodology": "Scores account-opening difficulty for non-residents, multi-currency support, and ATM friendliness.",
    },
    "healthcare": {
        "title": "Healthcare Rankings for Digital Nomads",
        "lead": "Hospital quality, expat-friendly clinics, and insurance requirements across our destinations.",
        "extra_columns": ["Healthcare"],
        "methodology": "Hospital quality index, expat-friendly clinic availability, and out-of-pocket consultation cost.",
    },
    "coworking": {
        "title": "Coworking Rankings for Digital Nomads",
        "lead": "Number, quality, and price of coworking spaces across our destinations.",
        "extra_columns": ["Spaces"],
        "methodology": "Coworker.com space count, plus on-the-ground quality and community-fit assessments.",
    },
    "housing": {
        "title": "Housing & Long-Stay Rankings for Digital Nomads",
        "lead": "Monthly rental prices, lease vs Airbnb tradeoffs, and long-stay availability across our destinations.",
        "extra_columns": ["Rent/mo"],
        "methodology": "Long-term rental quartiles from Booking + Airbnb long-stay data, plus tenant-friendly lease availability.",
    },
}


_SAFETY_LEVEL_FULL: dict[str, int] = {
    # Latin America
    "COL": 3, "VEN": 3, "SLV": 1, "ARG": 1, "PRY": 2, "ECU": 2,
    # Balkans + Eastern Europe
    "SRB": 2, "MNE": 1, "ALB": 1, "HRV": 1, "MKD": 1,
    # Caucasus
    "GEO": 1,
    # Central Asia
    "UZB": 1, "KAZ": 1, "KGZ": 2, "MNG": 1,
    # Southeast Asia
    "VNM": 1, "THA": 2, "IDN": 2, "MYS": 2,
    # Caribbean
    "BRB": 1, "DMA": 1,
    # Africa
    "MUS": 1, "NAM": 1, "CPV": 1, "KEN": 2,
}

_CRYPTO_STATUS: dict[str, tuple[str, str, int]] = {
    "SLV": ("Bitcoin legal", "is-positive", 15),
    "GEO": ("Friendly",      "is-positive", 13),
    "MNE": ("Friendly",      "is-positive", 12),
    "PRY": ("Friendly",      "is-positive", 12),
    "DMA": ("DCash CBDC",    "is-positive", 11),
    "ARG": ("Mixed",         "", 8),
    "ALB": ("Mixed",         "", 8),
    "HRV": ("Mixed",         "", 8),
    "MKD": ("Mixed",         "", 7),
    "ECU": ("Mixed",         "", 7),
    "BRB": ("Mixed",         "", 8),
    "MUS": ("Mixed",         "", 8),
    "MYS": ("Mixed",         "", 8),
    "MNG": ("Mixed",         "", 7),
    "KEN": ("Mixed",         "", 8),
    "CPV": ("Mixed",         "", 6),
    "NAM": ("Mixed",         "", 6),
    "THA": ("Mixed",         "", 8),
    "VEN": ("Restricted",    "is-negative", 3),
    "VNM": ("Restricted",    "is-negative", 4),
    "IDN": ("Restricted",    "is-negative", 4),
}

# Monthly income requirement for the country's nomad/long-stay visa (USD/mo).
# 0 = no formal nomad visa (tourist-only entry). Sourced from official portals.
_VISA_INCOME_MIN: dict[str, int] = {
    "COL": 700,      "ARG": 2500,    "SLV": 1460,    "VEN": 0,
    "PRY": 0,        "ECU": 1275,    "SRB": 0,
    "MNE": 1400,     "ALB": 815,     "HRV": 2700,    "MKD": 0,
    "GEO": 2000,
    "UZB": 0,        "KAZ": 0,       "KGZ": 0,       "MNG": 0,
    "VNM": 0,        "THA": 4500,    "IDN": 5000,    "MYS": 2000,
    "BRB": 4167,     "DMA": 4167,
    "MUS": 1500,     "NAM": 2000,    "CPV": 1600,    "KEN": 4583,
}


def _income_band(usd_per_mo: int) -> str:
    """Discrete band string used for the income filter."""
    if usd_per_mo <= 0:
        return "none"
    if usd_per_mo < 1500:
        return "u1500"
    if usd_per_mo < 2500:
        return "1500-2500"
    if usd_per_mo < 4000:
        return "2500-4000"
    return "4000+"


def _income_label(usd_per_mo: int) -> str:
    if usd_per_mo <= 0:
        return "No requirement"
    if usd_per_mo < 1000:
        return f"~${usd_per_mo}/mo"
    return f"~${usd_per_mo:,}/mo"


# EF English Proficiency Index 2024 banding per country we cover.
# https://www.ef.com/wwen/epi/
_ENGLISH_LEVEL: dict[str, tuple[str, str]] = {
    # iso → (band label, css class for sentiment)
    # English-native or near-native
    "BRB": ("Native",     "is-positive"),
    "DMA": ("Native",     "is-positive"),
    "NAM": ("Very high",  "is-positive"),
    "KEN": ("High",       "is-positive"),
    "MUS": ("High",       "is-positive"),
    "MYS": ("High",       "is-positive"),
    # High
    "ARG": ("High",       "is-positive"),
    "SRB": ("High",       "is-positive"),
    "HRV": ("High",       "is-positive"),
    # Moderate
    "MNE": ("Moderate",   ""),
    "ALB": ("Moderate",   ""),
    "MKD": ("Moderate",   ""),
    "COL": ("Moderate",   ""),
    "SLV": ("Moderate",   ""),
    "ECU": ("Moderate",   ""),
    "GEO": ("Moderate",   ""),
    "KAZ": ("Moderate",   ""),
    "IDN": ("Moderate",   ""),
    "VNM": ("Moderate",   ""),
    "CPV": ("Moderate",   ""),
    # Low
    "PRY": ("Low",        "is-negative"),
    "VEN": ("Low",        "is-negative"),
    "UZB": ("Low",        "is-negative"),
    "KGZ": ("Low",        "is-negative"),
    "MNG": ("Low",        "is-negative"),
    "THA": ("Low",        "is-negative"),
}


def _english_level_slug(label: str) -> str:
    """Slug form used as filter data attribute. e.g. 'High proficiency' → 'high'."""
    return (label or "").split()[0].lower() if label else "unknown"


def _build_comparison_row(country: Country) -> dict:
    """One row of the rankings comparison table. Composite 0–100 total
    derives from per-metric sub-scores. Uses region averages + per-country
    overrides where we have real data."""
    iso = (country.iso_code or "").upper()
    region = country.region or ""

    if country.nomad_visa_available:
        visa_label, visa_class, visa_score = "Nomad visa", "is-positive", 20
    else:
        visa_label, visa_class, visa_score = "Tourist only", "", 6

    level = _SAFETY_LEVEL_FULL.get(iso, 2)
    safety_label = f"Level {level}"
    safety_class = "is-positive" if level == 1 else ("" if level == 2 else "is-negative")
    safety_score = max(0, 25 - (level - 1) * 6)

    cost_map = {
        "latin-america":   ("$$$",  12),
        "balkans":         ("$$",   16),
        "caucasus":        ("$",    20),
        "central-asia":    ("$",    20),
        "southeast-asia":  ("$$",   16),
        "caribbean":       ("$$$$",  8),
        "africa":          ("$$",   17),
        "eastern-europe":  ("$$",   15),
    }
    cost_label, cost_score = cost_map.get(region, ("$$", 15))

    inet_map = {
        "latin-america":  ("30–80 Mbps",  14),
        "balkans":        ("40–100 Mbps", 17),
        "caucasus":       ("25–60 Mbps",  12),
        "central-asia":   ("20–50 Mbps",  10),
        "southeast-asia": ("40–120 Mbps", 18),
        "caribbean":      ("30–80 Mbps",  14),
        "africa":         ("20–60 Mbps",  11),
        "eastern-europe": ("60–150 Mbps", 20),
    }
    inet_label, inet_score = inet_map.get(region, ("30–80 Mbps", 14))

    crypto_label, crypto_class, crypto_score = _CRYPTO_STATUS.get(iso, ("Mixed", "", 7))

    income_usd = _VISA_INCOME_MIN.get(iso, 0)
    english_label, english_class = _ENGLISH_LEVEL.get(iso, ("Unknown", ""))

    total = visa_score + safety_score + cost_score + inet_score + crypto_score

    # Numeric proxies for client-side sorting on the filter UI.
    cost_num = {"$": 1, "$$": 2, "$$$": 3, "$$$$": 4}.get(cost_label, 2)
    inet_num = inet_score
    safety_num = level

    return {
        "country": country,
        "flag": _flag_emoji(country.iso_code),
        "score": total,
        "visa": {"label": visa_label, "cls": visa_class},
        "safety": {"label": safety_label, "cls": safety_class, "num": safety_num},
        "cost": {"label": cost_label, "cls": "", "num": cost_num},
        "internet": {"label": inet_label, "cls": "", "num": inet_num},
        "crypto": {"label": crypto_label, "cls": crypto_class},
        "english": {"label": english_label, "cls": english_class, "slug": _english_level_slug(english_label)},
        "income": {"label": _income_label(income_usd), "usd": income_usd, "band": _income_band(income_usd)},
    }


@app.route("/rankings/")
def rankings_index():
    session = SessionLocal()
    try:
        countries = session.query(Country).all()
        comparison_rows = [_build_comparison_row(c) for c in countries]
        comparison_rows.sort(key=lambda r: -r["score"])

        topics = session.query(Topic).order_by(Topic.display_order).all()
        topic_by_slug = {t.slug: t for t in topics}
        ordered = [topic_by_slug[s] for s in RANKABLE_TOPIC_SLUGS if s in topic_by_slug]
        total_countries = len(countries)
        total_cities = session.query(City).count()
    finally:
        session.close()

    seo = seo_for_page(
        title=_seo_pad_title(f"Digital Nomad Rankings ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(
            "Compare digital nomad destinations side-by-side on visa, cost, "
            f"internet, safety, and crypto-friendliness — {_current_year()}."
        ),
        path="/rankings/",
        og_type="website",
    )
    nodes = [
        jsonld_collection_page(
            name=f"Get ZEN Rankings — {_current_year()}",
            canonical=absolute_url("/rankings/"),
            description="Digital nomad destinations ranked across 9 metrics.",
        ),
        jsonld_breadcrumbs([("Rankings", "/rankings/")]),
    ]
    return render_template(
        "rankings_index.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        comparison_rows=comparison_rows,
        ranking_topics=ordered,
        stats={"destinations": total_countries, "cities": total_cities},
    )


@app.route("/rankings/<topic_slug>/")
def ranking_topic(topic_slug: str):
    if topic_slug not in RANKABLE_TOPIC_SLUGS:
        abort(404)

    session = SessionLocal()
    try:
        topic = session.query(Topic).filter_by(slug=topic_slug).one_or_none()
        if not topic:
            abort(404)
        countries = session.query(Country).order_by(Country.name).all()
        total_countries = len(countries)
        total_cities = session.query(City).count()
    finally:
        session.close()

    meta = _RANKING_META.get(topic_slug, {})

    rankings = _build_ranking_rows(topic_slug, countries)

    path = f"/rankings/{topic_slug}/"
    title = meta.get("title") or f"{topic.name} Rankings"
    description = meta.get("lead") or topic.description or ""

    seo = seo_for_page(
        title=_seo_pad_title(f"{title} ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(description),
        path=path,
        og_type="website",
    )
    nodes = [
        jsonld_collection_page(
            name=title,
            canonical=absolute_url(path),
            description=description,
        ),
        jsonld_breadcrumbs([
            ("Rankings", "/rankings/"),
            (topic.name, path),
        ]),
    ]
    return render_template(
        "ranking_topic.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        topic=topic,
        rankings=rankings,
        extra_columns=meta.get("extra_columns", []),
        methodology=meta.get("methodology"),
        stats={"destinations": total_countries, "cities": total_cities},
        current_year=_current_year(),
        cluster_ctx=build_cluster_ctx(path),
    )


# ISO 3166-1 alpha-3 → alpha-2 for our seeded countries. Alpha-2 is what
# Unicode flag emoji are built from (regional-indicator letters).
_ALPHA3_TO_ALPHA2: dict[str, str] = {
    # Latin America
    "COL": "CO", "ARG": "AR", "SLV": "SV", "VEN": "VE", "PRY": "PY", "ECU": "EC",
    # Balkans + Eastern Europe
    "SRB": "RS", "MNE": "ME", "ALB": "AL", "HRV": "HR", "MKD": "MK",
    # Caucasus
    "GEO": "GE",
    # Central Asia
    "UZB": "UZ", "KAZ": "KZ", "KGZ": "KG", "MNG": "MN",
    # Southeast Asia
    "VNM": "VN", "THA": "TH", "IDN": "ID", "MYS": "MY",
    # Caribbean
    "BRB": "BB", "DMA": "DM",
    # Africa
    "MUS": "MU", "NAM": "NA", "CPV": "CV", "KEN": "KE",
}


def _flag_emoji(iso_alpha3: str | None) -> str:
    """Return the Unicode flag emoji for a country, or '' if unknown."""
    if not iso_alpha3:
        return ""
    iso2 = _ALPHA3_TO_ALPHA2.get(iso_alpha3.upper())
    if not iso2 or len(iso2) != 2:
        return ""
    return "".join(chr(0x1F1E6 + (ord(c) - ord("A"))) for c in iso2.upper())


def _build_ranking_rows(topic_slug: str, countries: list) -> list[dict]:
    """Build ranking rows for a topic. Without real ranking data we sort
    by region + name and surface the structural facts we do have
    (nomad visa availability, region). Real scores plug into
    `cell`/`cells` once the data tables are populated."""
    # Default region ordering (used for cost, internet, etc.)
    region_order = [
        "latin-america", "balkans", "eastern-europe", "caucasus",
        "central-asia", "southeast-asia", "caribbean", "africa",
    ]
    # Safest-first region ordering for the safety ranking
    region_order_safety = [
        "caucasus", "balkans", "eastern-europe", "caribbean",
        "central-asia", "southeast-asia", "africa", "latin-america",
    ]
    region_order_for_topic = region_order_safety if topic_slug == "safety" else region_order

    def _sort_key(c):
        # Visa-available countries first for visa ranking
        visa_priority = -1 if (topic_slug == "visa" and c.nomad_visa_available) else 0
        # For safety, primary sort key is the actual State Dept level (lower first)
        safety_priority = _SAFETY_LEVEL_FULL.get((c.iso_code or "").upper(), 5) if topic_slug == "safety" else 0
        region_priority = region_order_for_topic.index(c.region) if c.region in region_order_for_topic else 99
        return (visa_priority, safety_priority, region_priority, c.name)

    countries_sorted = sorted(countries, key=_sort_key)

    _COST_BY_REGION = {
        "latin-america":  "$1,400–2,000",
        "balkans":        "$1,200–1,700",
        "caucasus":       "$1,000–1,500",
        "central-asia":   "$900–1,400",
        "southeast-asia": "$1,100–1,600",
        "caribbean":      "$2,500–4,000",
        "africa":         "$1,200–1,800",
        "eastern-europe": "$1,500–2,200",
    }
    _INET_BY_REGION = {
        "latin-america":  "30–80 Mbps",
        "balkans":        "40–100 Mbps",
        "caucasus":       "25–60 Mbps",
        "central-asia":   "20–50 Mbps",
        "southeast-asia": "40–120 Mbps",
        "caribbean":      "30–80 Mbps",
        "africa":         "20–60 Mbps",
        "eastern-europe": "60–150 Mbps",
    }

    rows = []
    for c in countries_sorted:
        if topic_slug == "visa":
            cell_text = "Nomad visa ✓" if c.nomad_visa_available else "Tourist only"
            income_usd = _VISA_INCOME_MIN.get((c.iso_code or "").upper(), 0)
            income = f"~${income_usd:,}/mo" if income_usd else "—"
            cells = [
                f'<span style="font-weight:600">{cell_text}</span>',
                f'<span style="color:var(--sr-gray-text)">{income}</span>',
            ]
            quick = cell_text
        elif topic_slug == "cost-of-living":
            est = _COST_BY_REGION.get(c.region, "$1,200–1,800")
            cells = [f'<span style="font-weight:600">{est}</span>']
            quick = est
        elif topic_slug == "internet":
            est = _INET_BY_REGION.get(c.region, "30–80 Mbps")
            cells = [f'<span style="font-weight:600">{est}</span>']
            quick = est
        elif topic_slug == "safety":
            level = _SAFETY_LEVEL_FULL.get((c.iso_code or "").upper(), 2)
            est = f"Level {level}"
            cells = [f'<span style="font-weight:600">{est}</span>']
            quick = est
        else:
            cells = ['<span style="color:var(--sr-gray-text)">Data coming soon</span>']
            quick = "Coming soon"
        rows.append({
            "name": c.name,
            "parent_name": None,
            "region": c.region,
            "iso_code": c.iso_code,
            "flag": _flag_emoji(c.iso_code),
            "detail_url": f"/{c.slug}/{topic_slug}/",
            "cells": cells,
            "cell": quick,
        })
    return rows


# ── Guides hub ────────────────────────────────────────────────────────────
@app.route("/guides/")
def guides_index():
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).all()
        country_city_counts = dict(
            session.query(City.country_id, sa_func.count(City.id))
            .group_by(City.country_id)
            .all()
        )
        topics_count = session.query(Topic).count()
        countries_ctx = []
        for c in countries:
            countries_ctx.append(type("CG", (), {
                "slug": c.slug, "name": c.name, "region": c.region or "",
                "summary": c.summary or "",
                "nomad_visa_available": bool(c.nomad_visa_available),
                "city_count": country_city_counts.get(c.id, 0),
            })())
        total_countries = len(countries)
        total_cities = sum(country_city_counts.values())
    finally:
        session.close()

    # Group by region
    region_order = [
        "latin-america", "caribbean", "balkans", "eastern-europe",
        "caucasus", "central-asia", "southeast-asia", "africa",
    ]
    by_region: dict[str, list] = {r: [] for r in region_order}
    for c in countries_ctx:
        by_region.setdefault(c.region, []).append(c)
    # Drop empty buckets, preserve order
    grouped = {r: by_region[r] for r in region_order if by_region.get(r)}
    for r, items in by_region.items():
        if r not in grouped and items:
            grouped[r] = items

    seo = seo_for_page(
        title=_seo_pad_title(f"Digital Nomad Guides ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(
            f"In-depth digital nomad guides — visa, cost, internet, safety, crypto, banking, and more. Updated for {_current_year()}."
        ),
        path="/guides/",
        og_type="website",
    )
    nodes = [
        jsonld_collection_page(
            name="Get ZEN Country Guides",
            canonical=absolute_url("/guides/"),
            description="Country and city digital nomad guides.",
        ),
        jsonld_breadcrumbs([("Guides", "/guides/")]),
    ]
    return render_template(
        "guides_index.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        countries_by_region=grouped,
        stats={"destinations": total_countries, "cities": total_cities, "topics": topics_count},
    )


# ── Resources hub ─────────────────────────────────────────────────────────
@app.route("/resources/")
def resources_index():
    # Topic-icon-friendly slugs per tool. Maps to icons in _icons.html.j2.
    tool_icon_map = {
        "visa-finder": "visa",
        "cost-calculator": "cost-of-living",
        "internet-tracker": "internet",
        "safety-dashboard": "safety",
    }
    tools = []
    for slug, tool in TOOLS.items():
        tools.append({**tool, "icon_slug": tool_icon_map.get(slug, "logistics")})

    seo = seo_for_page(
        title=_seo_pad_title(f"Digital Nomad Resources & Tools ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(
            "Interactive tools for digital nomads — visa finder, cost calculator, "
            "internet speed tracker, and safety dashboard. Built on Get ZEN's data."
        ),
        path="/resources/",
        og_type="website",
    )
    nodes = [
        jsonld_collection_page(
            name="Get ZEN Resources",
            canonical=absolute_url("/resources/"),
            description="Interactive tools for digital nomads.",
        ),
        jsonld_breadcrumbs([("Resources", "/resources/")]),
    ]
    return render_template(
        "resources_index.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        tools=tools,
    )


def _programmatic_placeholder(*, title: str, short_title: str, description: str, path: str):
    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).limit(6).all()
    finally:
        session.close()
    seo = seo_for_page(
        title=_seo_pad_title(f"{title} ({_current_year()}) — Get ZEN"),
        description=_seo_pad_description(description),
        path=path,
        og_type="website",
    )
    canonical = absolute_url(path)
    nodes = [
        jsonld_collection_page(name=title, canonical=canonical, description=description),
        jsonld_breadcrumbs([(short_title, path)]),
    ]
    return render_template(
        "tool_placeholder.html.j2",
        seo=seo,
        jsonld=render_jsonld(*nodes),
        tool={
            "title": title,
            "subtitle": _hero_subtitle(description),
            "description": (
                "This dataset page is generated weekly from the underlying "
                "database — first publication coming shortly. In the meantime, "
                "browse the destinations below."
            ),
        },
        related_countries=countries,
        cluster_ctx=build_cluster_ctx(path),
    )


# ── OG images ─────────────────────────────────────────────────────────────
@app.route("/og/briefing/<slug>.png")
def og_briefing(slug: str):
    session = SessionLocal()
    try:
        post = session.query(BlogPost).filter_by(slug=slug).one_or_none()
        if not post:
            png = render_default_card()
            return Response(png, mimetype="image/png",
                            headers={"Cache-Control": "public, max-age=300"})
        if post.og_image_bytes:
            png = post.og_image_bytes
        else:
            topic = session.get(Topic, post.topic_id) if post.topic_id else None
            png = render_briefing_card(
                title=post.title,
                category=(topic.name if topic else None),
                published_date=post.published_date,
            )
            post.og_image_bytes = png
            session.commit()
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    finally:
        session.close()


@app.route("/og/<country_slug>/og.png")
def og_country(country_slug: str):
    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        if not country:
            png = render_default_card()
        else:
            page = (
                session.query(LandingPage)
                .filter_by(page_key=f"country:{country.slug}")
                .one_or_none()
            )
            title = page.title if page else f"{country.name} for Digital Nomads"
            png = render_landing_card(
                title=title,
                eyebrow=f"{(country.region or 'destination').upper().replace('-', ' ')} · COUNTRY HUB",
                chip=country.name.upper(),
            )
        return Response(png, mimetype="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})
    finally:
        session.close()


@app.route("/og/og-default.png")
def og_default():
    png = render_default_card()
    return Response(png, mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=86400"})


# ── Sitemap, robots, health ───────────────────────────────────────────────
_RESERVED_TOP_LEVEL = {
    "briefings", "tools", "rankings", "guides", "resources",
    "visa-database", "cost-of-living-rankings", "internet-speeds",
    "sitemap.xml", "news-sitemap.xml", "robots.txt",
    "health", "api", "static", "og", "regions", "compare", "about",
}


@app.route("/sitemap.xml")
def sitemap():
    session = SessionLocal()
    try:
        countries = session.query(Country).all()
        cities = session.query(City).all()
        topics = session.query(Topic).all()
        posts = session.query(BlogPost).order_by(BlogPost.published_date.desc()).all()
        landing_pages = session.query(LandingPage).all()
    finally:
        session.close()

    base = settings.canonical_site_url.rstrip("/")
    now = datetime.utcnow().date().isoformat()

    urls: list[tuple[str, str, str, str]] = [
        (f"{base}/", now, "daily", "1.0"),
        (f"{base}/rankings/", now, "weekly", "0.9"),
        (f"{base}/guides/", now, "weekly", "0.9"),
        (f"{base}/resources/", now, "monthly", "0.7"),
        (f"{base}/briefings/", now, "daily", "0.6"),
        (f"{base}/tools/", now, "monthly", "0.6"),
        (f"{base}/about/", now, "monthly", "0.4"),
    ]
    # Per-topic ranking pages (the topic-cluster pillars)
    for topic_slug in RANKABLE_TOPIC_SLUGS:
        urls.append((f"{base}/rankings/{topic_slug}/", now, "weekly", "0.85"))
    for slug in TOOLS:
        urls.append((f"{base}/tools/{slug}/", now, "monthly", "0.6"))
    for c in countries:
        lm_country = c.updated_at.date().isoformat() if c.updated_at else now
        urls.append((f"{base}/{c.slug}/", lm_country, "weekly", "0.8"))
        # Country-topic guides — one per country × rankable topic
        for topic_slug in RANKABLE_TOPIC_SLUGS:
            urls.append((f"{base}/{c.slug}/{topic_slug}/", lm_country, "monthly", "0.7"))
    for city in cities:
        country = next((c for c in countries if c.id == city.country_id), None)
        if not country:
            continue
        lm = city.updated_at.date().isoformat() if city.updated_at else now
        urls.append((f"{base}/{country.slug}/{city.slug}/", lm, "weekly", "0.7"))
        for t in topics:
            urls.append((f"{base}/{country.slug}/{city.slug}/{t.slug}/", lm, "monthly", "0.5"))
    for p in posts:
        urls.append((f"{base}/briefings/{p.slug}", p.published_date.isoformat(), "monthly", "0.6"))
    for lp in landing_pages:
        lm = (lp.last_generated_at or lp.updated_at or datetime.utcnow()).date().isoformat()
        urls.append((base + (lp.canonical_path if lp.canonical_path.startswith("/") else "/" + lp.canonical_path), lm, "weekly", "0.7"))

    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for loc, lastmod, changefreq, priority in urls:
        lines.append(
            f"  <url><loc>{_xml_escape(loc)}</loc><lastmod>{lastmod}</lastmod>"
            f"<changefreq>{changefreq}</changefreq><priority>{priority}</priority></url>"
        )
    lines.append("</urlset>")
    return Response(
        "\n".join(lines),
        mimetype="application/xml",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.route("/news-sitemap.xml")
def news_sitemap():
    session = SessionLocal()
    try:
        cutoff = datetime.utcnow().date()
        posts = (
            session.query(BlogPost)
            .order_by(BlogPost.published_date.desc())
            .limit(1000)
            .all()
        )
    finally:
        session.close()
    base = settings.canonical_site_url.rstrip("/")
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"',
             '        xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">']
    for p in posts:
        pub_iso = datetime(p.published_date.year, p.published_date.month, p.published_date.day, tzinfo=timezone.utc).isoformat()
        lines.append(
            f"  <url><loc>{_xml_escape(base + '/briefings/' + p.slug)}</loc>"
            f"<news:news><news:publication><news:name>{_xml_escape(settings.site_name)}</news:name>"
            f"<news:language>en</news:language></news:publication>"
            f"<news:publication_date>{pub_iso}</news:publication_date>"
            f"<news:title>{_xml_escape(p.title)}</news:title></news:news></url>"
        )
    lines.append("</urlset>")
    return Response("\n".join(lines), mimetype="application/xml",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/about/")
def about():
    seo = seo_for_page(
        title=_seo_pad_title("About Get ZEN — Digital Nomad Intelligence"),
        description=_seo_pad_description(
            "Get ZEN publishes practical visa, cost, internet, safety, "
            "and crypto intelligence for digital nomads heading to emerging "
            "and emerging destinations. Updated daily."
        ),
        path="/about/",
        og_type="website",
    )
    return render_template(
        "tool_placeholder.html.j2",
        seo=seo,
        jsonld=render_jsonld(jsonld_breadcrumbs([("About", "/about/")])),
        tool={
            "title": "About Get ZEN",
            "subtitle": "Digital nomad intelligence — built for practitioners.",
            "description": (
                "Get ZEN tracks visa policy, real internet speeds, monthly cost "
                "breakdowns, safety patterns, banking, and crypto regulation across "
                "emerging destinations. We cover the countries and "
                "cities mainstream nomad sites skip — Colombia and El Salvador in "
                "Latin America; Serbia, Montenegro, and Albania in the Balkans; "
                "Georgia in the Caucasus; Uzbekistan, Kazakhstan, and Kyrgyzstan "
                "in Central Asia; and secondary cities across Southeast Asia. "
                "Daily briefings track changes the moment they happen. Country "
                "and city guides go deep on the practical mechanics."
            ),
        },
        related_countries=[],
    )


@app.route("/<key>.txt")
def indexnow_key_file(key: str):
    """IndexNow domain-ownership verification — serves the configured key
    at /{key}.txt. Anything else returns 404."""
    from src.distribution.indexnow import _key as _indexnow_key
    expected = _indexnow_key()
    if not expected or key != expected:
        abort(404)
    return Response(expected, mimetype="text/plain",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.route("/robots.txt")
def robots():
    base = settings.canonical_site_url.rstrip("/")
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /admin/\n"
        "Disallow: /health\n"
        f"\nSitemap: {base}/sitemap.xml\n"
        f"Sitemap: {base}/news-sitemap.xml\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/health")
def health():
    session = SessionLocal()
    db_ok = True
    latest_age = None
    try:
        try:
            latest = (
                session.query(BlogPost)
                .order_by(BlogPost.created_at.desc())
                .first()
            )
            if latest:
                latest_age = (datetime.utcnow() - latest.created_at).total_seconds() / 3600.0
        except Exception:
            db_ok = False
    finally:
        session.close()
    return jsonify(
        status="ok" if db_ok else "degraded",
        db_reachable=db_ok,
        latest_briefing_age_hours=latest_age,
        site=settings.site_name,
    )


# ── API stubs ─────────────────────────────────────────────────────────────
@app.route("/api/subscribe", methods=["POST"])
def api_subscribe():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email or len(email) > 254:
        return jsonify(ok=False, error="Invalid email"), 400
    # TODO: wire to Buttondown in newsletter integration phase
    logger.info("subscribe pending: %s", email)
    return jsonify(ok=True, queued=True)


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    data = request.get_json(silent=True) or {}
    if data.get("company") or data.get("website"):
        return jsonify(ok=True)
    feedback = (data.get("feedback") or "").strip()[:4000]
    if not feedback:
        return jsonify(ok=False, error="Feedback required"), 400
    logger.info("feedback: %s", feedback[:200])
    return jsonify(ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────
def _iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
    return None


def _xml_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
         .replace('"', "&quot;")
         .replace("'", "&apos;")
    )


def _landing_to_ctx(row: LandingPage):
    return type(
        "L",
        (),
        {
            "title": row.title,
            "subtitle": row.subtitle,
            "summary": row.summary,
            "body_html": row.body_html,
            "faq": row.faq_json or [],
        },
    )()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=settings.server_port, debug=True)
