"""Rendering helpers for Get ZEN — Jinja filters, JSON-LD builders, SEO context.

Keeps templates dumb. server.py imports `register_jinja` to attach filters to
the Flask app's Jinja env, then calls the seo_for_* and jsonld_for_* helpers
to build the dict passed into each page render.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

from src.config import settings


# ── Publisher display-name map ────────────────────────────────────────────
_KNOWN_SOURCE_DOMAINS: dict[str, str] = {
    "news.google.com": "Google News",
    "reuters.com": "Reuters",
    "bloomberg.com": "Bloomberg",
    "ft.com": "Financial Times",
    "wsj.com": "The Wall Street Journal",
    "nytimes.com": "The New York Times",
    "economist.com": "The Economist",
    "bbc.com": "BBC",
    "bbc.co.uk": "BBC",
    "apnews.com": "Associated Press",
    "aljazeera.com": "Al Jazeera",
    "cnbc.com": "CNBC",
    "forbes.com": "Forbes",
    "theguardian.com": "The Guardian",
    "ft.com": "Financial Times",
    # Nomad / expat / travel
    "nomadlist.com": "Nomad List",
    "numbeo.com": "Numbeo",
    "expatistan.com": "Expatistan",
    "coworker.com": "Coworker",
    "reddit.com": "Reddit",
    # Government
    "state.gov": "US State Department",
    "travel.state.gov": "US State Department",
    "gov.uk": "UK Government",
    "osac.gov": "OSAC",
}


def source_display_name(url: str | None) -> str:
    if not url:
        return "source"
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return "source"
    host = host[4:] if host.startswith("www.") else host
    if not host:
        return "source"
    if host in _KNOWN_SOURCE_DOMAINS:
        return _KNOWN_SOURCE_DOMAINS[host]
    parts = host.split(".")
    if len(parts) >= 3:
        tail = ".".join(parts[-2:])
        if tail in _KNOWN_SOURCE_DOMAINS:
            return _KNOWN_SOURCE_DOMAINS[tail]
    stem = parts[-2] if len(parts) >= 2 else parts[0]
    return stem.replace("-", " ").title()


# ── SERP-budget filters ───────────────────────────────────────────────────
def seo_title(s: str | None, max_len: int = 70) -> str:
    if not s:
        return s or ""
    s = " ".join(str(s).split())
    if len(s) <= max_len:
        return s
    return s[:max_len].rsplit(" ", 1)[0].rstrip(" ,;:—-")


def seo_desc(s: str | None, max_len: int = 160) -> str:
    if not s:
        return s or ""
    s = " ".join(str(s).split())
    if len(s) <= max_len:
        return s
    budget = max_len - 3
    for sep in (". ", "— ", "; ", ", "):
        idx = s[:budget].rfind(sep)
        if idx > budget // 2:
            return s[: idx + len(sep)].rstrip() + "..."
    return s[:budget].rsplit(" ", 1)[0].rstrip(" ,;:—-") + "..."


def register_jinja(app) -> None:
    """Wire filters + globals onto a Flask app's Jinja env."""
    app.jinja_env.filters["seo_title"] = seo_title
    app.jinja_env.filters["seo_desc"] = seo_desc
    app.jinja_env.globals["source_display_name"] = source_display_name
    app.jinja_env.globals["site_name"] = settings.site_name
    app.jinja_env.globals["site_locale"] = settings.site_locale
    app.jinja_env.globals["current_year"] = datetime.utcnow().year


# ── URL helpers ───────────────────────────────────────────────────────────
def absolute_url(path: str) -> str:
    base = settings.canonical_site_url.rstrip("/")
    if path.startswith(("http://", "https://")):
        return path
    if not path.startswith("/"):
        path = "/" + path
    return base + path


def og_image_for(path: str) -> str:
    """Default OG image. Override per-page when a real card exists.

    The default route renders a generic Get ZEN card on demand — no
    static file required.
    """
    return absolute_url("/og/og-default.png")


# ── JSON-LD builders ──────────────────────────────────────────────────────
def _publisher_node() -> dict:
    return {
        "@type": "Organization",
        "name": settings.site_owner_org,
        "url": settings.canonical_site_url,
        "logo": {
            "@type": "ImageObject",
            "url": absolute_url("/static/images/logo.png"),
        },
    }


def _organization_node() -> dict:
    return {
        "@type": "Organization",
        "name": settings.site_owner_org,
        "url": settings.canonical_site_url,
    }


def jsonld_website() -> dict:
    return {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "WebSite",
                "@id": settings.canonical_site_url + "/#website",
                "url": settings.canonical_site_url,
                "name": settings.site_name,
                "publisher": _publisher_node(),
            }
        ],
    }


def jsonld_breadcrumbs(crumbs: list[tuple[str, str]]) -> dict:
    """crumbs = [(name, path)] — leading Home omitted, added automatically."""
    items = [{"@type": "ListItem", "position": 1, "name": "Home", "item": settings.canonical_site_url + "/"}]
    for i, (name, path) in enumerate(crumbs, start=2):
        items.append(
            {
                "@type": "ListItem",
                "position": i,
                "name": name,
                "item": absolute_url(path),
            }
        )
    return {"@context": "https://schema.org", "@type": "BreadcrumbList", "itemListElement": items}


def jsonld_faq(faq: list[dict]) -> dict | None:
    pairs = [
        {
            "@type": "Question",
            "name": item["question"],
            "acceptedAnswer": {"@type": "Answer", "text": item["answer"]},
        }
        for item in (faq or [])
        if item.get("question") and item.get("answer")
    ]
    if len(pairs) < 2:
        return None
    return {"@context": "https://schema.org", "@type": "FAQPage", "mainEntity": pairs}


def jsonld_news_article(post, canonical: str, og_image: str) -> dict:
    headline = (post.title or "")[:110]
    published_iso = _iso(post.published_date)
    modified_iso = _iso(post.updated_at or post.created_at)
    keywords = post.keywords_json or []
    return {
        "@context": "https://schema.org",
        "@type": "NewsArticle",
        "headline": headline,
        "datePublished": published_iso,
        "dateModified": modified_iso,
        "url": canonical,
        "image": [og_image] if og_image else [],
        "wordCount": post.word_count or None,
        "author": _organization_node(),
        "publisher": _publisher_node(),
        "keywords": ", ".join(keywords) if isinstance(keywords, list) else (keywords or ""),
        "isAccessibleForFree": True,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
    }


def jsonld_landing(page, canonical: str, page_type: str = "WebPage") -> dict:
    return {
        "@context": "https://schema.org",
        "@type": page_type,
        "name": page.title,
        "description": page.summary or "",
        "url": canonical,
        "publisher": _publisher_node(),
        "datePublished": _iso(page.created_at),
        "dateModified": _iso(page.last_generated_at or page.updated_at),
    }


def jsonld_place_country(*, name: str, canonical: str, description: str,
                          iso_code: str | None = None) -> dict:
    """Fallback Schema.org Country node when no LandingPage row exists."""
    node = {
        "@context": "https://schema.org",
        "@type": "Country",
        "name": name,
        "description": description,
        "url": canonical,
    }
    if iso_code:
        node["identifier"] = {"@type": "PropertyValue", "propertyID": "iso-3166-1-alpha-3", "value": iso_code}
    return node


def jsonld_place_city(*, name: str, canonical: str, description: str,
                       country_name: str, lat: float | None = None,
                       lon: float | None = None) -> dict:
    node: dict = {
        "@context": "https://schema.org",
        "@type": "City",
        "name": name,
        "description": description,
        "url": canonical,
        "containedInPlace": {"@type": "Country", "name": country_name},
    }
    if lat is not None and lon is not None:
        node["geo"] = {"@type": "GeoCoordinates", "latitude": lat, "longitude": lon}
    return node


def jsonld_article_fallback(*, headline: str, canonical: str, description: str) -> dict:
    """Article-typed Schema.org node for topic pages without a LandingPage row."""
    return {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": headline[:110],
        "description": description,
        "url": canonical,
        "publisher": _publisher_node(),
        "isAccessibleForFree": True,
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
    }


def jsonld_collection_page(*, name: str, canonical: str, description: str) -> dict:
    """CollectionPage Schema.org node for index + programmatic pages."""
    return {
        "@context": "https://schema.org",
        "@type": "CollectionPage",
        "name": name,
        "description": description,
        "url": canonical,
        "publisher": _publisher_node(),
        "isPartOf": {"@type": "WebSite", "name": settings.site_name, "url": settings.canonical_site_url},
    }


def render_jsonld(*nodes) -> str:
    nodes = [n for n in nodes if n]
    if not nodes:
        return ""
    if len(nodes) == 1:
        return json.dumps(nodes[0], ensure_ascii=False, separators=(",", ":"))
    # Combine multiple via @graph
    return json.dumps(
        {"@context": "https://schema.org", "@graph": nodes},
        ensure_ascii=False,
        separators=(",", ":"),
    )


# ── SEO context builders ──────────────────────────────────────────────────
def _iso(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.replace(tzinfo=timezone.utc).isoformat()
    if isinstance(d, date):
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).isoformat()
    return None


def seo_for_page(
    *,
    title: str,
    description: str,
    path: str,
    og_image: str | None = None,
    og_type: str = "website",
    keywords: Iterable[str] | None = None,
    news_keywords: Iterable[str] | None = None,
    published_iso: str | None = None,
    modified_iso: str | None = None,
    section: str | None = None,
    article_tags: Iterable[str] | None = None,
) -> dict:
    canonical = absolute_url(path)
    return {
        "title": title,
        "description": description,
        "canonical": canonical,
        "og_image": og_image or og_image_for(path),
        "og_type": og_type,
        "site_name": settings.site_name,
        "locale": settings.site_locale,
        "keywords": ", ".join(keywords) if keywords else "",
        "news_keywords": ", ".join(news_keywords) if news_keywords else "",
        "published_iso": published_iso,
        "modified_iso": modified_iso,
        "section": section,
        "article_tags": list(article_tags or []),
        "robots": "index, follow, max-image-preview:large, max-snippet:-1",
    }


# ── HTML sanitization (allowlist) ─────────────────────────────────────────
_ALLOWED_TAGS = {"h2", "h3", "h4", "p", "ul", "ol", "li", "strong", "em", "b", "i", "blockquote", "a", "br", "hr"}
_DISALLOWED_RE = re.compile(
    r"<\s*(?P<close>/?)\s*(?P<tag>[a-zA-Z][a-zA-Z0-9]*)\b[^>]*>",
)


def sanitize_html(html: str) -> str:
    """Allowlist-strip tags. Conservative — only the ones we generate."""
    if not html:
        return ""

    def repl(m: re.Match) -> str:
        tag = m.group("tag").lower()
        if tag in _ALLOWED_TAGS:
            return m.group(0)
        return ""

    return _DISALLOWED_RE.sub(repl, html)
