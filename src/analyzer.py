"""LLM-powered nomad-relevance analysis for scraped articles.

Reads SCRAPED articles, scores each on nomad relevance, extracts country/
city/topic slugs (which the pipeline resolves to FK ids), and persists the
structured result in analysis_json.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date, timedelta

from openai import OpenAI

from src.config import settings
from src.models import (
    SessionLocal,
    ExternalArticleEntry,
    ArticleStatus,
    SourceType,
    Country,
    City,
    Topic,
)

logger = logging.getLogger(__name__)

LLM_CALL_BUDGET_PER_RUN = settings.llm_call_budget_per_run

_LLM_USAGE = {"calls": 0, "input_tokens": 0, "output_tokens": 0}


def reset_usage() -> None:
    _LLM_USAGE.update({"calls": 0, "input_tokens": 0, "output_tokens": 0})


def get_usage() -> dict:
    in_cost = _LLM_USAGE["input_tokens"] / 1_000_000 * settings.llm_input_price_per_mtok
    out_cost = _LLM_USAGE["output_tokens"] / 1_000_000 * settings.llm_output_price_per_mtok
    return {**_LLM_USAGE, "estimated_cost_usd": round(in_cost + out_cost, 4)}


# Pre-screen keywords. Articles that pass this run through the LLM;
# others get a rule-based "below threshold" stamp at zero LLM cost.
RELEVANCE_KEYWORDS = (
    "nomad", "expat", "remote work", "remote worker", "remote-work",
    "work remotely", "working remotely", "work from anywhere",
    "visa", "residency", "long stay", "long-stay", "permit",
    "cost of living", "costs of living", "cheapest", "cheap places",
    "rent", "housing", "rentals",
    "internet speed", "broadband", "wifi", "fiber",
    "coworking", "co-working",
    "safety", "advisory", "crime", "kidnap", "scam",
    "crypto", "bitcoin", "stablecoin", "tax",
    "currency control", "capital control", "inflation",
    "digital nomad visa", "e-residency", "freelance visa",
    "border", "immigration", "move abroad", "relocate",
    "livable", "liveable", "best places", "best countries",
)


SYSTEM_PROMPT_TEMPLATE = """You are a senior analyst for "Get ZEN," a site providing practical intelligence for digital nomads, remote workers, and expats heading to emerging destinations. Audience: experienced remote workers and crypto-native nomads who want signal, not travel-blog fluff.

Return ONLY a JSON object with these exact fields:
{{
  "relevance_score": <int 1-10>,
  "country_slug": "<one of: {country_slugs} | null>",
  "city_slug": "<one of: {city_slugs} | null>",
  "topic_slug": "<one of: {topic_slugs} | null>",
  "sentiment": "<positive|neutral|cautionary|negative>",
  "category_label": "<short label, e.g. 'Visa Policy', 'Safety Alert', 'Cost Shift', 'Crypto Regulation', 'Internet Quality'>",
  "headline_short": "<max 80 chars>",
  "takeaway": "<2-4 sentences for an experienced nomad; wrap the key sentence in <strong>. No markdown.>",
  "is_breaking": <bool>,
  "is_actionable": <bool>,
  "source_trust": "<official|tier1|tier2|community>"
}}

SCORING:
- 8-10: directly affects nomads in a specific country/city — new visa programs, visa policy changes, safety incidents in nomad areas, sudden cost shifts, crypto regulation changes, major internet/infrastructure events.
- 6-7: meaningful background — economic shifts, banking changes, regional policy trends, infrastructure investments.
- 4-5: useful context — political/economic news from covered countries with indirect nomad implications.
- 1-3: irrelevant — generic tourism, domestic politics with no nomad angle, business/investor news, sports, entertainment.

DEPRIORITIZE: domestic political bickering, sports, entertainment, business M&A, generic tourist content.

If the article mentions a specific country we cover, set country_slug. If it mentions a specific city, set city_slug (only if it matches a city in our list). topic_slug picks the single best-fit category.

If the article is not about any country/city we cover, set them to null but still score for general nomad relevance (e.g. global crypto regulation, generic visa programs).

Return ONLY the JSON object."""


USER_PROMPT_TEMPLATE = """Analyze this article for digital-nomad relevance:

SOURCE: {source_name} ({credibility})
DATE: {published_date}
COUNTRY HINT: {country_hint}
CITY HINT: {city_hint}
HEADLINE: {headline}
URL: {source_url}

SNIPPET / BODY:
{body_text}"""


def _load_taxonomy() -> tuple[list[str], list[str], list[str], dict[str, int], dict[tuple[str, str], int], dict[str, int]]:
    session = SessionLocal()
    try:
        countries = session.query(Country).all()
        cities = session.query(City).all()
        topics = session.query(Topic).all()

        country_slug_to_id = {c.slug: c.id for c in countries}
        country_id_to_slug = {c.id: c.slug for c in countries}
        city_slug_to_id = {
            (country_id_to_slug[ci.country_id], ci.slug): ci.id
            for ci in cities
            if ci.country_id in country_id_to_slug
        }
        topic_slug_to_id = {t.slug: t.id for t in topics}

        return (
            sorted(country_slug_to_id.keys()),
            sorted({s for _, s in city_slug_to_id.keys()}),
            sorted(topic_slug_to_id.keys()),
            country_slug_to_id,
            city_slug_to_id,
            topic_slug_to_id,
        )
    finally:
        session.close()


def run_analysis() -> dict:
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — skipping analysis")
        return {"analyzed": 0, "skipped": 0, "errors": 0}

    (
        country_slugs,
        city_slugs,
        topic_slugs,
        country_slug_to_id,
        city_slug_to_id_by_pair,
        topic_slug_to_id,
    ) = _load_taxonomy()

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        country_slugs=", ".join(country_slugs) or "none",
        city_slugs=", ".join(city_slugs) or "none",
        topic_slugs=", ".join(topic_slugs) or "none",
    )

    client = OpenAI(api_key=settings.openai_api_key)
    db = SessionLocal()
    reset_usage()
    summary = {"analyzed": 0, "skipped": 0, "errors": 0}

    try:
        articles = (
            db.query(ExternalArticleEntry)
            .filter(ExternalArticleEntry.status == ArticleStatus.SCRAPED)
            .filter(
                ExternalArticleEntry.published_date
                >= date.today() - timedelta(days=settings.report_lookback_days)
            )
            .all()
        )
        logger.info("Analysis queue: %d articles", len(articles))

        rule_based, llm_candidates = _partition(articles)
        logger.info(
            "Partitioned: %d rule-based, %d LLM candidates | budget=%d",
            len(rule_based), len(llm_candidates), LLM_CALL_BUDGET_PER_RUN,
        )

        # Rule-based: cheap stamp for keyword-misses
        for article in rule_based:
            try:
                analysis = _rule_based_analysis(article)
                article.analysis_json = analysis
                article.relevance_score = analysis.get("relevance_score", 2)
                article.status = ArticleStatus.ANALYZED
                summary["analyzed"] += 1
            except Exception as e:
                logger.error("Rule-based analysis failed for %d: %s", article.id, e)
                summary["errors"] += 1
        db.commit()

        # LLM pass
        budget = LLM_CALL_BUDGET_PER_RUN
        for article in llm_candidates:
            if budget <= 0:
                summary["skipped"] += 1
                continue
            try:
                analysis = _analyze_article(
                    client,
                    system_prompt=system_prompt,
                    headline=article.headline,
                    body_text=(article.body_text or "") or _snippet_from_metadata(article.extra_metadata),
                    source_name=article.source_name or "Unknown",
                    credibility=article.credibility.value if article.credibility else "tier2",
                    published_date=str(article.published_date),
                    source_url=article.source_url,
                    country_hint=_country_hint(article.country_id, country_slugs, country_slug_to_id),
                    city_hint=_city_hint(article.city_id, city_slug_to_id_by_pair),
                )
                article.analysis_json = analysis
                article.relevance_score = float(analysis.get("relevance_score") or 0)

                # Resolve slugs back to FK ids — only override if the LLM
                # actually returned a slug, never wipe a scraper-set hint.
                slug = analysis.get("country_slug")
                if slug and slug in country_slug_to_id:
                    article.country_id = country_slug_to_id[slug]
                    cslug = analysis.get("city_slug")
                    if cslug:
                        cid = city_slug_to_id_by_pair.get((slug, cslug))
                        if cid:
                            article.city_id = cid
                tslug = analysis.get("topic_slug")
                if tslug and tslug in topic_slug_to_id:
                    article.topic_id = topic_slug_to_id[tslug]

                article.status = ArticleStatus.ANALYZED
                db.commit()
                summary["analyzed"] += 1
                budget -= 1
                logger.info(
                    "LLM analyzed [budget %d]: %s (score=%s, %s/%s/%s)",
                    budget,
                    article.headline[:60],
                    analysis.get("relevance_score", "?"),
                    analysis.get("country_slug"),
                    analysis.get("city_slug"),
                    analysis.get("topic_slug"),
                )
            except Exception as e:
                logger.error("Analysis failed for %d: %s", article.id, e)
                summary["errors"] += 1
                db.rollback()
            time.sleep(0.3)

        db.commit()
    finally:
        db.close()

    usage = get_usage()
    summary["llm_usage"] = usage
    logger.info(
        "Analysis complete: analyzed=%d skipped=%d errors=%d | calls=%d in=%d out=%d cost=$%.4f",
        summary["analyzed"], summary["skipped"], summary["errors"],
        usage["calls"], usage["input_tokens"], usage["output_tokens"],
        usage["estimated_cost_usd"],
    )
    return summary


# ── helpers ───────────────────────────────────────────────────────────────
def _partition(articles: list) -> tuple[list, list]:
    rule_based, llm_candidates = [], []
    for a in articles:
        if _passes_prefilter(a):
            llm_candidates.append(a)
        else:
            rule_based.append(a)
    llm_candidates.sort(key=_llm_priority, reverse=True)
    return rule_based, llm_candidates


def _passes_prefilter(article) -> bool:
    body = article.body_text or ""
    snippet = ""
    if article.extra_metadata:
        snippet = article.extra_metadata.get("snippet") or ""
    text = f"{article.headline or ''} {body} {snippet}".lower()
    return any(kw in text for kw in RELEVANCE_KEYWORDS)


def _llm_priority(article) -> tuple:
    source_rank = {
        SourceType.VISA_PORTAL: 4,
        SourceType.SAFETY_ADVISORY: 4,
        SourceType.GOVERNMENT_NEWS: 3,
        SourceType.CRYPTO_REGULATIONS: 3,
        SourceType.GOOGLE_NEWS: 2,
        SourceType.NOMAD_FORUMS: 2,
    }.get(article.source, 1)
    has_country_hint = 1 if article.country_id else 0
    return (source_rank, has_country_hint, article.published_date or date.min)


def _rule_based_analysis(article) -> dict:
    return {
        "relevance_score": 2,
        "country_slug": None,
        "city_slug": None,
        "topic_slug": None,
        "sentiment": "neutral",
        "category_label": "Background",
        "headline_short": (article.headline or "")[:80],
        "takeaway": "Filtered below relevance threshold by pre-screen.",
        "is_breaking": False,
        "is_actionable": False,
        "source_trust": (article.credibility.value if article.credibility else "tier2"),
        "_rule_based": True,
    }


def _snippet_from_metadata(meta: dict | None) -> str:
    if not meta:
        return ""
    return (meta.get("snippet") or "")[:1000]


def _country_hint(country_id: int | None, country_slugs: list[str], slug_to_id: dict[str, int]) -> str:
    if not country_id:
        return "none"
    for slug, cid in slug_to_id.items():
        if cid == country_id:
            return slug
    return "none"


def _city_hint(city_id: int | None, pair_to_id: dict[tuple[str, str], int]) -> str:
    if not city_id:
        return "none"
    for (_, cslug), cid in pair_to_id.items():
        if cid == city_id:
            return cslug
    return "none"


def _analyze_article(
    client: OpenAI,
    *,
    system_prompt: str,
    headline: str,
    body_text: str,
    source_name: str,
    credibility: str,
    published_date: str,
    source_url: str,
    country_hint: str,
    city_hint: str,
) -> dict:
    body_truncated = body_text[:3000] if body_text else "(no body text available — analyse from the headline + snippet only)"
    user_msg = USER_PROMPT_TEMPLATE.format(
        source_name=source_name,
        credibility=credibility,
        published_date=published_date,
        headline=headline,
        source_url=source_url,
        body_text=body_truncated,
        country_hint=country_hint,
        city_hint=city_hint,
    )
    response = client.chat.completions.create(
        model=settings.openai_model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.3,
        max_tokens=500,
        response_format={"type": "json_object"},
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        _LLM_USAGE["calls"] += 1
        _LLM_USAGE["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        _LLM_USAGE["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0

    return json.loads(response.choices[0].message.content)
