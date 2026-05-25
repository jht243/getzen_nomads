"""Long-form nomad briefing generator.

For each ANALYZED ExternalArticleEntry with relevance_score >= threshold and
no existing BlogPost, runs a single LLM call producing a 700-900 word
briefing with H2 sections, takeaways, keywords, and internal links to
country/city/topic landing pages.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from typing import Optional

from openai import OpenAI

from src.analyzer import _LLM_USAGE
from src.config import settings
from src.style_guide import render_style_guide, current_year
from src.models import (
    BlogPost,
    ExternalArticleEntry,
    ArticleStatus,
    SessionLocal,
    Country,
    City,
    Topic,
    init_db,
)

logger = logging.getLogger(__name__)


def _build_system_prompt() -> str:
    """Compose the briefing system prompt from the shared style guide
    plus briefing-specific structure rules. Called per-run so the
    embedded year is always current."""
    style = render_style_guide()
    return f"""{style}

### Briefing-specific structure
You are writing a DAILY BRIEFING — a 800–1,100-word post about ONE recent development affecting digital nomads in one country/city. Voice: one nomad advising another — practical, source-driven, skeptical when warranted. Name caveats, flag uncertainty.

Use these H2 sections in order (no others):
1. (no heading) Lead paragraph — the news, the specific number/date/program, what it means for a nomad reading right now.
2. <h2>What Happened</h2> — 2-3 paragraphs of context and source background.
3. <h2>What It Means for Nomads</h2> — visa mechanics, cost shifts, neighborhoods affected, infrastructure changes.
4. <h2>The Practical Take</h2> — concrete steps a nomad can take this week.
5. <h2>The Bigger Picture</h2> — 1 paragraph tying to a wider regional trend.

### JSON output
Return ONLY a JSON object with these fields:
- title (50-60 chars, includes primary keyword + year where natural)
- subtitle (80-130 chars, expands the title)
- summary (120-150 chars, plain text meta description)
- body_html (the briefing — H2 structure above, sanitized HTML only)
- primary_keyword (the main keyword you targeted)
- keywords (8-12 lowercase phrases — mix head and long-tail; include city/country + year + "{current_year()}")
- key_takeaways (3-5 plain-text bullet sentences — each a concrete, actionable insight)
"""


SYSTEM_PROMPT_FALLBACK = "You are a senior writer for Get ZEN. Apply E-E-A-T, 7th-8th grade readability, and produce valid JSON."


# Topic-keyed link presets. The generator merges these with country/city
# specific paths at runtime, so each post can pull 4-6 candidates and the
# LLM picks 2-3.
_TOPIC_GENERIC_LINKS: dict[str, list[tuple[str, str]]] = {
    "visa": [
        ("/rankings/visa/", "the digital nomad visa database"),
        ("/tools/visa-finder/", "filter visas by income and duration"),
    ],
    "cost-of-living": [
        ("/rankings/cost-of-living/", "monthly costs across destinations"),
        ("/tools/cost-calculator/", "build your monthly nomad budget"),
    ],
    "internet": [
        ("/rankings/internet/", "internet speeds by city"),
        ("/tools/internet-tracker/", "live internet reliability tracker"),
    ],
    "safety": [
        ("/tools/safety-dashboard/", "country-by-country safety scoring"),
    ],
    "crypto": [
        ("/rankings/visa/", "visas friendly to crypto-native nomads"),
    ],
    "banking": [
        ("/rankings/visa/", "residency programs with banking access"),
    ],
}


USER_PROMPT_TEMPLATE = """Write a long-form briefing about the development below, from the perspective of an experienced digital nomad reader.

SOURCE: {source_name} ({credibility})
PUBLISHED: {published_date}
URL: {source_url}
HEADLINE: {headline}

COUNTRY: {country_label}
CITY: {city_label}
TOPIC: {topic_label}

ANALYST CATEGORY: {category_label}
ANALYST TAKEAWAY:
{takeaway}

RELEVANCE SCORE: {relevance}/10
SENTIMENT: {sentiment}

SOURCE BODY (truncated):
{body_text}

INTERNAL LINK TARGETS (use 2-3 of these as <a href="/path/"> in body text with natural anchor text):
{internal_links}

Follow the 5-section body structure from your instructions. 800-1000 words. Be specific: name the visa, price, requirement, neighborhood, mbps figure — never be vague. Lead with what changed and what to do."""


_ALLOWED_TAGS_RE = re.compile(
    r"<\s*/?\s*(h2|h3|h4|p|ul|ol|li|strong|em|b|i|blockquote|a)(\s+[^>]*)?\s*/?\s*>",
    re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


def _slugify(text: str, *, max_len: int = 80) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return slug[:max_len] or "briefing"


def _count_words(html: str) -> int:
    text = _ANY_TAG_RE.sub(" ", html or "")
    return len([w for w in text.split() if w])


def _sanitize_body_html(html: str) -> str:
    if not html:
        return ""

    def _replace(match: re.Match) -> str:
        if _ALLOWED_TAGS_RE.fullmatch(match.group(0)):
            return match.group(0)
        return ""

    return _ANY_TAG_RE.sub(_replace, html)


def _candidate_articles(db) -> list[ExternalArticleEntry]:
    cutoff = date.today() - timedelta(days=settings.blog_gen_lookback_days)
    rows = (
        db.query(ExternalArticleEntry)
        .filter(ExternalArticleEntry.status == ArticleStatus.ANALYZED)
        .filter(ExternalArticleEntry.published_date >= cutoff)
        .order_by(ExternalArticleEntry.published_date.desc())
        .all()
    )
    out = []
    for r in rows:
        score = (r.relevance_score or 0)
        if score < settings.blog_gen_min_relevance:
            continue
        out.append(r)
    return out


def _existing_blog_keys(db) -> set[tuple[str, int]]:
    return {
        (row.source_table, row.source_id)
        for row in db.query(BlogPost.source_table, BlogPost.source_id).all()
    }


def _build_internal_links(
    *,
    country_slug: Optional[str],
    country_name: Optional[str],
    city_slug: Optional[str],
    city_name: Optional[str],
    topic_slug: Optional[str],
) -> list[tuple[str, str]]:
    links: list[tuple[str, str]] = []
    if country_slug:
        links.append((f"/{country_slug}/", f"{country_name or country_slug} nomad guide"))
    if country_slug and city_slug:
        links.append((f"/{country_slug}/{city_slug}/", f"the {city_name or city_slug} guide"))
    if country_slug and city_slug and topic_slug:
        links.append((f"/{country_slug}/{city_slug}/{topic_slug}/", f"{topic_slug.replace('-', ' ')} in {city_name or city_slug}"))
    if topic_slug:
        links.extend(_TOPIC_GENERIC_LINKS.get(topic_slug, []))
    # Always-useful fallbacks
    links.append(("/briefings/", "the daily briefings feed"))
    # Deduplicate while preserving order
    seen = set()
    unique = []
    for path, anchor in links:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, anchor))
    return unique[:6]


def run_blog_generation() -> dict:
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — skipping blog generation")
        return {"generated": 0, "skipped": 0, "errors": 0}

    init_db()
    client = OpenAI(api_key=settings.openai_api_key)
    db = SessionLocal()
    summary = {"generated": 0, "skipped": 0, "errors": 0}

    try:
        candidates = _candidate_articles(db)
        existing = _existing_blog_keys(db)
        candidates = [
            a for a in candidates
            if ("external_articles", a.id) not in existing
        ]
        # Highest relevance first
        candidates.sort(key=lambda a: (a.relevance_score or 0, a.published_date), reverse=True)

        logger.info("Blog candidates: %d (budget=%d)", len(candidates), settings.blog_gen_budget_per_run)

        # Pre-load taxonomy labels (one query each)
        countries = {c.id: c for c in db.query(Country).all()}
        cities = {c.id: c for c in db.query(City).all()}
        topics = {t.id: t for t in db.query(Topic).all()}

        budget = settings.blog_gen_budget_per_run
        for article in candidates:
            if budget <= 0:
                summary["skipped"] += 1
                continue

            country = countries.get(article.country_id) if article.country_id else None
            city = cities.get(article.city_id) if article.city_id else None
            topic = topics.get(article.topic_id) if article.topic_id else None

            internal_links = _build_internal_links(
                country_slug=country.slug if country else None,
                country_name=country.name if country else None,
                city_slug=city.slug if city else None,
                city_name=city.name if city else None,
                topic_slug=topic.slug if topic else None,
            )

            try:
                post_data = _generate_post(
                    client,
                    article=article,
                    country_label=(country.name if country else "global"),
                    city_label=(city.name if city else "—"),
                    topic_label=(topic.name if topic else "—"),
                    internal_links=internal_links,
                )
            except Exception as e:
                logger.error("Generation failed for article %d: %s", article.id, e)
                summary["errors"] += 1
                continue

            try:
                body_html = _sanitize_body_html(post_data.get("body_html") or "")
                if _count_words(body_html) < 400:
                    logger.warning(
                        "Article %d body too short (%d words) — skipping",
                        article.id, _count_words(body_html),
                    )
                    summary["errors"] += 1
                    continue

                slug = _make_unique_slug(db, post_data.get("title") or article.headline)
                analysis = article.analysis_json or {}
                takeaways = post_data.get("key_takeaways") or []
                keywords = post_data.get("keywords") or []

                post = BlogPost(
                    source_table="external_articles",
                    source_id=article.id,
                    slug=slug,
                    title=(post_data.get("title") or article.headline)[:300],
                    subtitle=(post_data.get("subtitle") or "")[:400],
                    summary=(post_data.get("summary") or "")[:300],
                    body_html=body_html,
                    country_id=article.country_id,
                    city_id=article.city_id,
                    topic_id=article.topic_id,
                    keywords_json=keywords,
                    takeaways_json=takeaways,
                    word_count=_count_words(body_html),
                    reading_minutes=max(1, _count_words(body_html) // 200),
                    published_date=article.published_date or date.today(),
                    canonical_source_url=article.source_url,
                    llm_model=settings.openai_model,
                )
                db.add(post)
                article.status = ArticleStatus.APPROVED
                db.commit()
                summary["generated"] += 1
                budget -= 1
                logger.info(
                    "Generated [budget %d]: %s",
                    budget, post.title[:80],
                )
            except Exception as e:
                logger.error("Persist failed for article %d: %s", article.id, e)
                db.rollback()
                summary["errors"] += 1

    finally:
        db.close()

    logger.info(
        "Blog generation complete: generated=%d skipped=%d errors=%d",
        summary["generated"], summary["skipped"], summary["errors"],
    )
    return summary


def _make_unique_slug(db, title: str) -> str:
    base = _slugify(title)
    candidate = base
    i = 2
    while db.query(BlogPost.id).filter_by(slug=candidate).first():
        candidate = f"{base}-{i}"
        i += 1
    return candidate


def _generate_post(
    client: OpenAI,
    *,
    article: ExternalArticleEntry,
    country_label: str,
    city_label: str,
    topic_label: str,
    internal_links: list[tuple[str, str]],
) -> dict:
    analysis = article.analysis_json or {}
    body_text = article.body_text or ""
    if not body_text and article.extra_metadata:
        body_text = (article.extra_metadata.get("snippet") or "")
    body_truncated = body_text[:3000] if body_text else "(no body text available — write from headline + analyst takeaway)"

    links_str = "\n".join(f"- {path} — {anchor}" for path, anchor in internal_links)

    user_msg = USER_PROMPT_TEMPLATE.format(
        source_name=article.source_name or "Unknown",
        credibility=article.credibility.value if article.credibility else "tier2",
        published_date=str(article.published_date),
        source_url=article.source_url,
        headline=article.headline,
        country_label=country_label,
        city_label=city_label,
        topic_label=topic_label,
        category_label=analysis.get("category_label") or "—",
        takeaway=analysis.get("takeaway") or "—",
        relevance=int(analysis.get("relevance_score") or 0),
        sentiment=analysis.get("sentiment") or "neutral",
        body_text=body_truncated,
        internal_links=links_str,
    )

    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": _build_system_prompt()},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.4,
        max_tokens=2200,
        response_format={"type": "json_object"},
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        _LLM_USAGE["calls"] += 1
        _LLM_USAGE["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        _LLM_USAGE["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0

    return json.loads(response.choices[0].message.content)
