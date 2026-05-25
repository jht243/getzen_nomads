"""Evergreen landing-page generator.

Produces 2,000–4,000-word HTML for country hubs, city guides, topic deep-spokes,
and programmatic-database pages. Premium model. FAQ extracted as structured
JSON so the renderer can emit both a visible accordion and FAQPage JSON-LD.

Style guide applied via src/style_guide.render_style_guide() — keeps every
generator on the same SEO/E-E-A-T/readability contract.

Optional web grounding: when `landing_gen_web_search_enabled` is true and the
OpenAI Responses API is reachable, the generator augments prompts with a
web-search step. Off by default so cost stays predictable; flip on per-run.

Costs (gpt-5.2 premium pricing, no web search):
    ~3.5k input tokens + ~4k output tokens per page
    -> ~$0.018 input + $0.060 output = ~$0.08 / page
    -> 14 country hubs ≈ $1.10 / regen pass
    -> 30 city guides   ≈ $2.40 / regen pass
    -> 330 topic pages  ≈ $26 / full regen — meter this carefully.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date
from typing import Iterable, Optional

from openai import OpenAI

from src.analyzer import _LLM_USAGE
from src.config import settings
from src.models import (
    SessionLocal,
    Country,
    City,
    Topic,
    LandingPage,
    init_db,
)
from src.seo.cluster_topology import (
    cluster_for,
    other_members,
    pillar_link_for,
    invalidate_cache,
)
from src.style_guide import render_style_guide, current_year


logger = logging.getLogger(__name__)


_ALLOWED_TAGS_RE = re.compile(
    r"<\s*/?\s*(h2|h3|h4|p|ul|ol|li|strong|em|b|i|blockquote|a|table|thead|tbody|tr|th|td)(\s+[^>]*)?\s*/?\s*>",
    re.IGNORECASE,
)
_ANY_TAG_RE = re.compile(r"<[^>]+>")


# ── System prompts ────────────────────────────────────────────────────────
def _system_prompt_for(page_type: str) -> str:
    style = render_style_guide()
    type_specific = {
        "country": (
            "You are writing a COUNTRY HUB (pillar page) — the definitive Get ZEN "
            "guide to digital-nomad life in this country. Cover ALL of: "
            "visa landscape, where nomads cluster (cities + neighborhoods), "
            "monthly cost ranges by category with USD figures, internet quality "
            "and providers, safety patterns by region, banking + crypto access, "
            "healthcare quality and insurance, tax residency basics, and a "
            "practical move-in checklist. HARD MINIMUM: 2,000 words in body_html. "
            "Aim for 2,500–3,500 words. Use at least 7 H2 sections."
        ),
        "city": (
            "You are writing a CITY GUIDE (spoke page) — the on-the-ground "
            "Get ZEN guide to one specific city. Cover ALL of: cost of living "
            "with concrete monthly USD figures by category (rent, groceries, "
            "transport, dining, coworking, utilities), internet speeds with "
            "specific Mbps figures and providers, named coworking spaces and "
            "wifi cafés, safety by neighborhood with specific area names, "
            "expat/nomad community size and meetups, and the practical "
            "move-in mechanics (SIM card, residency registration, banking). "
            "HARD MINIMUM: 1,800 words in body_html. Aim for 2,000–2,800 "
            "words. Use at least 6 H2 sections."
        ),
        "topic": (
            "You are writing a TOPIC DEEP-SPOKE — one topic, one city, one "
            "country. Cover this topic in depth for nomads in this exact city: "
            "specific data, prices, regulations, named providers and "
            "neighborhoods. HARD MINIMUM: 1,000 words in body_html. Aim for "
            "1,200–2,000 words. Use at least 4 H2 sections."
        ),
        "country-topic": (
            "You are writing a COUNTRY-LEVEL TOPIC GUIDE — one topic, one "
            "country. Cover this topic at the national level: national policy, "
            "average / by-city data ranges, the official agencies or "
            "regulators involved, and how the cities we cover compare on this "
            "topic. Link to specific city deep-spokes for granular detail. "
            "HARD MINIMUM: 1,200 words in body_html. Aim for 1,500–2,200 "
            "words. Use at least 5 H2 sections."
        ),
        "programmatic": (
            "You are writing a PROGRAMMATIC DATABASE LANDING — a cross-country "
            "comparison page driven by structured data. Lead with a comparison "
            "table, then explain methodology and how to read the data. Target: "
            "1,500–2,500 words."
        ),
    }.get(page_type, "")

    return f"""{style}

### Page-specific guidance
{type_specific}

### JSON output
Return ONLY a JSON object with these exact fields:
{{
  "title": "<H1 / page title, 50-70 chars, includes primary keyword>",
  "seo_title": "<50-60 chars exactly, includes primary keyword + year>",
  "subtitle": "<one sentence, 80-160 chars>",
  "summary": "<120-160 chars plain-text meta description>",
  "body_html": "<the full article HTML following the structure + style above>",
  "primary_keyword": "<the main keyword you targeted>",
  "secondary_keywords": ["<5-10 supporting keywords>"],
  "faq": [
    {{"question": "<question>", "answer": "<2-4 sentence answer optimized for featured snippets>"}}
  ]
}}

The faq array MUST contain 4-6 items. Questions should be the ones a real nomad would type into Google about this exact topic — phrased naturally, not as keyword stuffing. Answers must be self-contained (no "as mentioned above")."""


# ── Helpers ───────────────────────────────────────────────────────────────
def _sanitize_body_html(html: str) -> str:
    if not html:
        return ""

    def _replace(match: re.Match) -> str:
        if _ALLOWED_TAGS_RE.fullmatch(match.group(0)):
            return match.group(0)
        return ""

    return _ANY_TAG_RE.sub(_replace, html)


def _count_words(html: str) -> int:
    text = _ANY_TAG_RE.sub(" ", html or "")
    return len([w for w in text.split() if w])


def _internal_link_targets_for(*, page_type: str, country: Optional[Country],
                                city: Optional[City], topic: Optional[Topic],
                                topics_all: list[Topic], other_cities: list[City]) -> list[tuple[str, str]]:
    """Build the link palette the LLM picks from. Drawn from the cluster
    topology so anchor text is consistent."""
    links: list[tuple[str, str]] = []

    if page_type == "country" and country:
        # Children: cities in this country, programmatic pages, briefings.
        for ci in other_cities[:8]:
            links.append((f"/{country.slug}/{ci.slug}/",
                          f"the {ci.name} nomad guide"))
        for t in topics_all[:6]:
            # If the topic has a programmatic pillar, use it; otherwise
            # skip — country page should not link into a single city's
            # topic deep-spoke as that's narrower than the country pillar.
            from src.seo.cluster_topology import _TOPIC_PILLAR_PATH
            pillar = _TOPIC_PILLAR_PATH.get(t.slug)
            if pillar:
                links.append((pillar, f"{t.name.lower()} across all destinations"))
        links.append(("/briefings/", f"daily {country.name} briefings and visa updates"))

    elif page_type == "city" and country and city:
        # Parent country hub, sibling cities, topic deep-spokes, programmatic pages.
        links.append((f"/{country.slug}/", f"the {country.name} country guide"))
        for ci in other_cities[:4]:
            links.append((f"/{country.slug}/{ci.slug}/", f"{ci.name} nomad guide"))
        for t in topics_all[:6]:
            links.append((f"/{country.slug}/{city.slug}/{t.slug}/",
                          f"{t.name.lower()} in {city.name}"))
        links.append(("/rankings/visa/", f"the {country.name} visa database"))
        links.append(("/rankings/cost-of-living/",
                      "cost of living rankings across destinations"))

    elif page_type == "topic" and country and city and topic:
        # Parent city + country, topic-cluster pillar, sibling topics in same city.
        links.append((f"/{country.slug}/{city.slug}/",
                      f"the {city.name} city guide"))
        links.append((f"/{country.slug}/", f"the {country.name} country guide"))
        from src.seo.cluster_topology import _TOPIC_PILLAR_PATH
        pillar = _TOPIC_PILLAR_PATH.get(topic.slug)
        if pillar:
            links.append((pillar, f"{topic.name.lower()} across all Get ZEN destinations"))
        # 3 sibling topics in this city for cross-linking
        for t in topics_all:
            if t.slug != topic.slug:
                links.append((f"/{country.slug}/{city.slug}/{t.slug}/",
                              f"{t.name.lower()} in {city.name}"))
        links.append(("/briefings/", f"latest {country.name} briefings"))

    elif page_type == "programmatic":
        links.append(("/briefings/", "daily Get ZEN briefings"))
        for ci in other_cities[:4]:
            country_slug = next(
                (c.slug for c in [country] if c) or ([cc for cc in [] if cc.id == ci.country_id] or [None]),
                None,
            )

    # Dedup, preserve order
    seen = set()
    unique: list[tuple[str, str]] = []
    for path, anchor in links:
        if path in seen:
            continue
        seen.add(path)
        unique.append((path, anchor))
    return unique[:14]


# ── Generation core ───────────────────────────────────────────────────────
def _generate(
    client: OpenAI,
    *,
    page_type: str,
    primary_keyword: str,
    seed_title: str,
    context_block: str,
    internal_links: list[tuple[str, str]],
    use_web_search: bool,
) -> dict:
    links_str = "\n".join(f"- {path} — {anchor}" for path, anchor in internal_links)
    user_msg = f"""Write the Get ZEN landing page for this target:

PRIMARY KEYWORD: {primary_keyword}
SEED TITLE (improve if you can, keep meaning): {seed_title}
YEAR: {current_year()}

CONTEXT (verified seed data — incorporate, do not contradict):
{context_block}

INTERNAL LINK TARGETS (pick 2-5; use exact paths):
{links_str}

Apply the full Get ZEN style guide. Output the JSON object only."""

    system_prompt = _system_prompt_for(page_type)
    model = settings.openai_premium_model
    in_price = settings.llm_premium_input_price_per_mtok
    out_price = settings.llm_premium_output_price_per_mtok

    # The Responses API + web_search_preview is the lever for E-E-A-T grounding.
    # It costs more and adds latency, so it's opt-in.
    if use_web_search:
        try:
            response = client.responses.create(
                model=model,
                instructions=system_prompt,
                input=user_msg,
                tools=[{"type": "web_search_preview"}],
                temperature=0.4,
                max_output_tokens=8000,
            )
            text = response.output_text
            usage = getattr(response, "usage", None)
            if usage is not None:
                _LLM_USAGE["calls"] += 1
                _LLM_USAGE["input_tokens"] += getattr(usage, "input_tokens", 0) or 0
                _LLM_USAGE["output_tokens"] += getattr(usage, "output_tokens", 0) or 0
            return _parse_json_payload(text), model, in_price, out_price
        except Exception as exc:
            logger.warning("Web-search-enabled generation failed (%s) — falling back to chat completion", exc)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        temperature=0.4,
        max_tokens=8000,
        response_format={"type": "json_object"},
    )
    usage = getattr(response, "usage", None)
    if usage is not None:
        _LLM_USAGE["calls"] += 1
        _LLM_USAGE["input_tokens"] += getattr(usage, "prompt_tokens", 0) or 0
        _LLM_USAGE["output_tokens"] += getattr(usage, "completion_tokens", 0) or 0
    return _parse_json_payload(response.choices[0].message.content), model, in_price, out_price


def _parse_json_payload(raw: str) -> dict:
    raw = (raw or "").strip()
    # Strip code fences defensively
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


# ── Persistence ───────────────────────────────────────────────────────────
def _upsert_landing(
    *,
    page_key: str,
    page_type: str,
    canonical_path: str,
    country_id: Optional[int],
    city_id: Optional[int],
    topic_id: Optional[int],
    payload: dict,
    model: str,
    in_price: float,
    out_price: float,
) -> LandingPage:
    body_html = _sanitize_body_html(payload.get("body_html") or "")
    wc = _count_words(body_html)

    keywords = list(filter(None, [payload.get("primary_keyword")])) + (payload.get("secondary_keywords") or [])
    faq = payload.get("faq") or []
    sections = []

    session = SessionLocal()
    try:
        row = (
            session.query(LandingPage)
            .filter_by(page_key=page_key)
            .one_or_none()
        )
        if row is None:
            row = LandingPage(page_key=page_key)
            session.add(row)
        row.page_type = page_type
        row.canonical_path = canonical_path
        row.title = (payload.get("seo_title") or payload.get("title") or "")[:400]
        row.subtitle = (payload.get("subtitle") or "")[:500]
        row.summary = (payload.get("summary") or "")[:300]
        row.body_html = body_html
        row.keywords_json = keywords
        row.sections_json = sections
        row.faq_json = faq
        row.country_id = country_id
        row.city_id = city_id
        row.topic_id = topic_id
        row.word_count = wc
        row.llm_model = model
        # Token totals come from the module-level usage accumulator; we
        # only persist model + cost here so per-row cost = the delta since
        # the last commit, not a precise per-call attribution.
        row.llm_cost_usd = None
        session.commit()
        session.refresh(row)
        return row
    finally:
        session.close()


# ── Public API ────────────────────────────────────────────────────────────
def generate_country_hub(country_slug: str, *, use_web_search: bool = False) -> Optional[LandingPage]:
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — cannot generate country hub")
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    init_db()

    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        if not country:
            raise ValueError(f"Unknown country slug: {country_slug}")
        cities = session.query(City).filter_by(country_id=country.id).order_by(City.name).all()
        topics_all = session.query(Topic).order_by(Topic.display_order).all()
    finally:
        session.close()

    primary_kw = f"{country.name} digital nomad guide {current_year()}"
    seed_title = f"{country.name} for Digital Nomads ({current_year()}): Visa, Cost, Internet, Safety"
    cities_brief = "\n".join(
        f"  - {c.name} (slug: {c.slug})" + (f" — {c.summary}" if c.summary else "")
        for c in cities
    ) or "  (no cities seeded yet)"
    context = (
        f"COUNTRY: {country.name} ({country.iso_code or '—'})\n"
        f"REGION: {country.region or '—'}\n"
        f"NOMAD VISA AVAILABLE: {country.nomad_visa_available}\n"
        f"SEED SUMMARY: {country.summary or '—'}\n"
        f"CITIES WE COVER:\n{cities_brief}\n"
        f"TOPICS WE COVER: {', '.join(t.name for t in topics_all)}"
    )
    links = _internal_link_targets_for(
        page_type="country",
        country=country, city=None, topic=None,
        topics_all=topics_all, other_cities=cities,
    )

    payload, model, in_price, out_price = _generate(
        client,
        page_type="country",
        primary_keyword=primary_kw,
        seed_title=seed_title,
        context_block=context,
        internal_links=links,
        use_web_search=use_web_search,
    )
    return _upsert_landing(
        page_key=f"country:{country.slug}",
        page_type="country",
        canonical_path=f"/{country.slug}/",
        country_id=country.id, city_id=None, topic_id=None,
        payload=payload, model=model, in_price=in_price, out_price=out_price,
    )


def generate_city_guide(country_slug: str, city_slug: str, *, use_web_search: bool = False) -> Optional[LandingPage]:
    if not settings.openai_api_key:
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    init_db()

    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        if not country:
            raise ValueError(f"Unknown country slug: {country_slug}")
        city = session.query(City).filter_by(country_id=country.id, slug=city_slug).one_or_none()
        if not city:
            raise ValueError(f"Unknown city slug: {country_slug}/{city_slug}")
        sibling_cities = (
            session.query(City)
            .filter(City.country_id == country.id, City.id != city.id)
            .order_by(City.name)
            .all()
        )
        topics_all = session.query(Topic).order_by(Topic.display_order).all()
    finally:
        session.close()

    primary_kw = f"{city.name} digital nomad guide {current_year()}"
    seed_title = f"{city.name}, {country.name} for Digital Nomads ({current_year()})"
    context = (
        f"CITY: {city.name}, {country.name}\n"
        f"LAT/LON: {city.lat}, {city.lon}\n"
        f"NOMAD-VISA AVAILABLE IN COUNTRY: {country.nomad_visa_available}\n"
        f"CITY SEED SUMMARY: {city.summary or '—'}\n"
        f"COUNTRY SEED SUMMARY: {country.summary or '—'}\n"
        f"TOPICS WE COVER: {', '.join(t.name for t in topics_all)}"
    )
    links = _internal_link_targets_for(
        page_type="city",
        country=country, city=city, topic=None,
        topics_all=topics_all, other_cities=sibling_cities,
    )
    payload, model, in_price, out_price = _generate(
        client,
        page_type="city",
        primary_keyword=primary_kw,
        seed_title=seed_title,
        context_block=context,
        internal_links=links,
        use_web_search=use_web_search,
    )
    return _upsert_landing(
        page_key=f"city:{country.slug}:{city.slug}",
        page_type="city",
        canonical_path=f"/{country.slug}/{city.slug}/",
        country_id=country.id, city_id=city.id, topic_id=None,
        payload=payload, model=model, in_price=in_price, out_price=out_price,
    )


def generate_topic_page(country_slug: str, city_slug: str, topic_slug: str,
                         *, use_web_search: bool = False) -> Optional[LandingPage]:
    if not settings.openai_api_key:
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    init_db()

    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        city = session.query(City).filter_by(country_id=country.id, slug=city_slug).one_or_none() if country else None
        topic = session.query(Topic).filter_by(slug=topic_slug).one_or_none()
        if not (country and city and topic):
            raise ValueError(f"Unknown path: /{country_slug}/{city_slug}/{topic_slug}/")
        sibling_cities = (
            session.query(City)
            .filter(City.country_id == country.id, City.id != city.id)
            .order_by(City.name)
            .all()
        )
        topics_all = session.query(Topic).order_by(Topic.display_order).all()
    finally:
        session.close()

    primary_kw = f"{topic.name.lower()} {city.name} {current_year()}"
    seed_title = f"{topic.name} in {city.name}, {country.name} ({current_year()})"
    context = (
        f"TOPIC: {topic.name} ({topic.slug})\n"
        f"TOPIC DESCRIPTION: {topic.description or '—'}\n"
        f"CITY: {city.name}, {country.name}\n"
        f"CITY SEED SUMMARY: {city.summary or '—'}\n"
        f"COUNTRY SEED SUMMARY: {country.summary or '—'}"
    )
    links = _internal_link_targets_for(
        page_type="topic",
        country=country, city=city, topic=topic,
        topics_all=topics_all, other_cities=sibling_cities,
    )
    payload, model, in_price, out_price = _generate(
        client,
        page_type="topic",
        primary_keyword=primary_kw,
        seed_title=seed_title,
        context_block=context,
        internal_links=links,
        use_web_search=use_web_search,
    )
    return _upsert_landing(
        page_key=f"topic:{country.slug}:{city.slug}:{topic.slug}",
        page_type="topic",
        canonical_path=f"/{country.slug}/{city.slug}/{topic.slug}/",
        country_id=country.id, city_id=city.id, topic_id=topic.id,
        payload=payload, model=model, in_price=in_price, out_price=out_price,
    )


def generate_country_topic_guide(country_slug: str, topic_slug: str,
                                  *, use_web_search: bool = False) -> Optional[LandingPage]:
    """Country-level topic guide — e.g. /colombia/visa/."""
    if not settings.openai_api_key:
        return None
    client = OpenAI(api_key=settings.openai_api_key)
    init_db()

    session = SessionLocal()
    try:
        country = session.query(Country).filter_by(slug=country_slug).one_or_none()
        topic = session.query(Topic).filter_by(slug=topic_slug).one_or_none()
        if not (country and topic):
            raise ValueError(f"Unknown path: /{country_slug}/{topic_slug}/")
        cities = (
            session.query(City)
            .filter(City.country_id == country.id)
            .order_by(City.name)
            .all()
        )
        topics_all = session.query(Topic).order_by(Topic.display_order).all()
    finally:
        session.close()

    primary_kw = f"{topic.name.lower()} {country.name} {current_year()}"
    seed_title = f"{topic.name} in {country.name} ({current_year()}): A Digital Nomad's Guide"
    cities_brief = "\n".join(f"  - {c.name}" for c in cities) or "  (no cities seeded)"
    context = (
        f"TOPIC: {topic.name} ({topic.slug})\n"
        f"TOPIC DESCRIPTION: {topic.description or '—'}\n"
        f"COUNTRY: {country.name} ({country.iso_code or '—'})\n"
        f"REGION: {country.region or '—'}\n"
        f"NOMAD VISA AVAILABLE: {country.nomad_visa_available}\n"
        f"COUNTRY SEED SUMMARY: {country.summary or '—'}\n"
        f"CITIES WE COVER:\n{cities_brief}"
    )
    # Build link palette: parent country hub, topic-cluster pillar, every
    # city-topic deep-spoke for this country, plus a sibling topic.
    links: list[tuple[str, str]] = [
        (f"/{country.slug}/", f"the {country.name} country guide"),
    ]
    from src.seo.cluster_topology import _TOPIC_PILLAR_PATH
    pillar = _TOPIC_PILLAR_PATH.get(topic.slug)
    if pillar:
        links.append((pillar, f"{topic.name.lower()} rankings across all destinations"))
    for c in cities[:6]:
        links.append((f"/{country.slug}/{c.slug}/{topic.slug}/",
                      f"{topic.name.lower()} in {c.name}"))
    links.append(("/rankings/", "the master rankings comparison"))

    payload, model, in_price, out_price = _generate(
        client,
        page_type="country-topic",
        primary_keyword=primary_kw,
        seed_title=seed_title,
        context_block=context,
        internal_links=links,
        use_web_search=use_web_search,
    )
    return _upsert_landing(
        page_key=f"country-topic:{country.slug}:{topic.slug}",
        page_type="country-topic",
        canonical_path=f"/{country.slug}/{topic.slug}/",
        country_id=country.id, city_id=None, topic_id=topic.id,
        payload=payload, model=model, in_price=in_price, out_price=out_price,
    )


def regenerate_all(
    *,
    page_types: Iterable[str] = ("country", "city", "country-topic", "topic"),
    budget: Optional[int] = None,
    use_web_search: Optional[bool] = None,
) -> dict:
    # Default to the global setting (which now defaults to True per the
    # editorial brief — "research the topic using search tools").
    if use_web_search is None:
        use_web_search = settings.landing_gen_web_search_enabled
    """Bulk-regenerate landing pages with a hard call budget.

    Prioritisation: country hubs > city guides > topic pages. Within each
    type, missing pages come before existing pages.
    """
    if not settings.openai_api_key:
        logger.error("OPENAI_API_KEY not set — cannot bulk-generate")
        return {"generated": 0, "errors": 0}

    init_db()
    invalidate_cache()  # in case seed data changed since last import

    summary = {"generated": 0, "skipped": 0, "errors": 0, "by_type": {}}

    session = SessionLocal()
    try:
        countries = session.query(Country).order_by(Country.name).all()
        cities = session.query(City).order_by(City.name).all()
        topics = session.query(Topic).order_by(Topic.display_order).all()
        existing_keys = {row.page_key for row in session.query(LandingPage.page_key).all()}
    finally:
        session.close()

    country_by_id = {c.id: c.slug for c in countries}
    queue: list[tuple[str, tuple]] = []
    if "country" in page_types:
        for c in countries:
            queue.append(("country", (c.slug,)))
    if "city" in page_types:
        for ci in cities:
            if ci.country_id in country_by_id:
                queue.append(("city", (country_by_id[ci.country_id], ci.slug)))
    if "country-topic" in page_types:
        # Only the 9 rankable topics — the others (logistics, scam-prevention)
        # are too thin for a country-level page.
        from src.seo.cluster_topology import RANKABLE_TOPIC_SLUGS
        rankable = set(RANKABLE_TOPIC_SLUGS)
        for c in countries:
            for t in topics:
                if t.slug in rankable:
                    queue.append(("country-topic", (c.slug, t.slug)))
    if "topic" in page_types:
        for ci in cities:
            if ci.country_id not in country_by_id:
                continue
            for t in topics:
                queue.append(("topic", (country_by_id[ci.country_id], ci.slug, t.slug)))

    def _key_for(item):
        ptype, args = item
        if ptype == "country":
            return f"country:{args[0]}"
        if ptype == "city":
            return f"city:{args[0]}:{args[1]}"
        if ptype == "country-topic":
            return f"country-topic:{args[0]}:{args[1]}"
        return f"topic:{args[0]}:{args[1]}:{args[2]}"

    # Missing pages first, within each type
    queue.sort(key=lambda item: (0 if _key_for(item) not in existing_keys else 1))

    remaining = budget if budget is not None else len(queue)
    for ptype, args in queue:
        if remaining <= 0:
            summary["skipped"] += 1
            continue
        try:
            if ptype == "country":
                generate_country_hub(args[0], use_web_search=use_web_search)
            elif ptype == "city":
                generate_city_guide(args[0], args[1], use_web_search=use_web_search)
            elif ptype == "country-topic":
                generate_country_topic_guide(args[0], args[1], use_web_search=use_web_search)
            else:
                generate_topic_page(args[0], args[1], args[2], use_web_search=use_web_search)
            summary["generated"] += 1
            summary["by_type"][ptype] = summary["by_type"].get(ptype, 0) + 1
            remaining -= 1
            logger.info("[%d left] generated %s %s", remaining, ptype, "/".join(args))
        except Exception as exc:
            logger.exception("Generation failed for %s %s: %s", ptype, args, exc)
            summary["errors"] += 1
            remaining -= 1

    logger.info("Landing-page regen complete: %s", summary)
    return summary
