import enum
from datetime import datetime
from threading import Lock

from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Text,
    Float,
    Date,
    DateTime,
    Enum,
    Boolean,
    JSON,
    LargeBinary,
    ForeignKey,
    UniqueConstraint,
    Index,
)
from sqlalchemy import inspect as sa_inspect, text as sa_text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

from src.config import settings

Base = declarative_base()


def _snake_case(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and not name[i - 1].isupper():
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def _enum_values(enum_cls):
    return Enum(
        enum_cls,
        values_callable=lambda x: [e.value for e in x],
        name=_snake_case(enum_cls.__name__),
    )


class SourceType(str, enum.Enum):
    GOOGLE_NEWS         = "google_news"
    VISA_PORTAL         = "visa_portal"
    SAFETY_ADVISORY     = "safety_advisory"
    SPEEDTEST           = "speedtest"
    COST_OF_LIVING      = "cost_of_living"
    CRYPTO_REGULATIONS  = "crypto_regulations"
    HOUSING             = "housing"
    COWORKING           = "coworking"
    NOMAD_FORUMS        = "nomad_forums"
    GOVERNMENT_NEWS     = "government_news"
    HEALTHCARE          = "healthcare"


class CredibilityTier(str, enum.Enum):
    OFFICIAL = "official"
    TIER1 = "tier1"
    TIER2 = "tier2"
    COMMUNITY = "community"


class ArticleStatus(str, enum.Enum):
    SCRAPED  = "scraped"
    ANALYZED = "analyzed"
    APPROVED = "approved"
    SENT     = "sent"


# ─────────────────────────────────────────────────────────────────────────
# Geo + taxonomy
# ─────────────────────────────────────────────────────────────────────────

class Country(Base):
    __tablename__ = "countries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    iso_code = Column(String(3), nullable=True, index=True)
    region = Column(String(60), nullable=True, index=True)
    currency = Column(String(10), nullable=True)
    languages_json = Column(JSON, nullable=True)
    nomad_visa_available = Column(Boolean, default=False, index=True)
    overall_score = Column(Float, nullable=True)
    summary = Column(Text, nullable=True)
    extra_metadata = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    cities = relationship("City", back_populates="country", cascade="all, delete-orphan")


class City(Base):
    __tablename__ = "cities"
    __table_args__ = (UniqueConstraint("country_id", "slug", name="uq_country_city_slug"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=False, index=True)
    slug = Column(String(80), nullable=False, index=True)
    name = Column(String(120), nullable=False)
    lat = Column(Float, nullable=True)
    lon = Column(Float, nullable=True)
    population = Column(Integer, nullable=True)

    nomad_score = Column(Float, nullable=True)
    internet_avg_mbps = Column(Float, nullable=True)
    cost_index = Column(Float, nullable=True)         # monthly nomad budget USD
    safety_score = Column(Float, nullable=True)       # 0-10
    summary = Column(Text, nullable=True)
    extra_metadata = Column(JSON, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    country = relationship("Country", back_populates="cities")


class Topic(Base):
    __tablename__ = "topics"

    id = Column(Integer, primary_key=True, autoincrement=True)
    slug = Column(String(80), nullable=False, unique=True, index=True)
    name = Column(String(120), nullable=False)
    display_order = Column(Integer, default=0)
    icon = Column(String(40), nullable=True)
    description = Column(Text, nullable=True)


# ─────────────────────────────────────────────────────────────────────────
# Content: scraped articles + generated briefings + landing pages
# ─────────────────────────────────────────────────────────────────────────

class ExternalArticleEntry(Base):
    """Articles from external sources (Google News, visa portals, advisories, etc.)."""

    __tablename__ = "external_articles"
    __table_args__ = (UniqueConstraint("source", "source_url", name="uq_ext_source_url"),)

    id = Column(Integer, primary_key=True, autoincrement=True)

    source = Column(_enum_values(SourceType), nullable=False, index=True)
    source_url = Column(String(1000), nullable=False)
    source_name = Column(String(200), nullable=True)
    credibility = Column(_enum_values(CredibilityTier), default=CredibilityTier.TIER2)

    headline = Column(Text, nullable=False)
    published_date = Column(Date, nullable=False, index=True)
    body_text = Column(Text, nullable=True)
    article_type = Column(String(100), nullable=True)

    # Geographic + topical association (filled in by analyzer)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=True, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True, index=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True, index=True)

    relevance_score = Column(Float, nullable=True)
    tone_score = Column(Float, nullable=True)
    extra_metadata = Column(JSON, nullable=True)

    analysis_json = Column(JSON, nullable=True)
    status = Column(_enum_values(ArticleStatus), default=ArticleStatus.SCRAPED)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class BlogPost(Base):
    """Long-form LLM-generated briefing tied to one ExternalArticleEntry."""

    __tablename__ = "blog_posts"
    __table_args__ = (
        UniqueConstraint("source_table", "source_id", name="uq_blog_source"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    source_table = Column(String(50), nullable=False, index=True)
    source_id = Column(Integer, nullable=False, index=True)

    slug = Column(String(200), nullable=False, unique=True, index=True)
    title = Column(Text, nullable=False)
    subtitle = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)
    body_html = Column(Text, nullable=False)

    # Pre-rendered 1200x630 OG card PNG, served by /og/briefing/<slug>.png
    og_image_bytes = Column(LargeBinary, nullable=True)

    country_id = Column(Integer, ForeignKey("countries.id"), nullable=True, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True, index=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True, index=True)

    keywords_json = Column(JSON, nullable=True)
    related_slugs_json = Column(JSON, nullable=True)
    takeaways_json = Column(JSON, nullable=True)

    word_count = Column(Integer, nullable=True)
    reading_minutes = Column(Integer, nullable=True)

    published_date = Column(Date, nullable=False, index=True)
    canonical_source_url = Column(String(1000), nullable=True)

    llm_model = Column(String(100), nullable=True)
    llm_input_tokens = Column(Integer, nullable=True)
    llm_output_tokens = Column(Integer, nullable=True)
    llm_cost_usd = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LandingPage(Base):
    """Evergreen landing pages — country hubs, city guides, topic pages, region hubs."""

    __tablename__ = "landing_pages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    page_key = Column(String(160), nullable=False, unique=True, index=True)
    page_type = Column(String(40), nullable=False, index=True)  # country | city | topic | region | comparison | programmatic

    title = Column(Text, nullable=False)
    subtitle = Column(Text, nullable=True)
    summary = Column(Text, nullable=True)       # used as meta description
    body_html = Column(Text, nullable=False)
    keywords_json = Column(JSON, nullable=True)
    sections_json = Column(JSON, nullable=True)
    faq_json = Column(JSON, nullable=True)

    country_id = Column(Integer, ForeignKey("countries.id"), nullable=True, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True, index=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True, index=True)

    canonical_path = Column(String(240), nullable=False)
    word_count = Column(Integer, nullable=True)

    llm_model = Column(String(120), nullable=True)
    llm_input_tokens = Column(Integer, nullable=True)
    llm_output_tokens = Column(Integer, nullable=True)
    llm_cost_usd = Column(Float, nullable=True)

    last_generated_at = Column(DateTime, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────
# Structured data tables (visa info, cost breakdowns, internet measurements)
# ─────────────────────────────────────────────────────────────────────────

class VisaInfo(Base):
    __tablename__ = "visa_info"
    __table_args__ = (
        UniqueConstraint("country_id", "visa_type", name="uq_visa_country_type"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=False, index=True)
    visa_type = Column(String(120), nullable=False)            # e.g. "Digital Nomad Visa"
    visa_slug = Column(String(120), nullable=True)
    duration_days = Column(Integer, nullable=True)
    extendable = Column(Boolean, default=False)
    cost_usd = Column(Float, nullable=True)
    min_income_usd = Column(Float, nullable=True)               # monthly income requirement
    requirements_json = Column(JSON, nullable=True)
    application_url = Column(String(500), nullable=True)
    notes = Column(Text, nullable=True)
    source_url = Column(String(500), nullable=True)
    last_verified_date = Column(Date, nullable=True, index=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CostBreakdown(Base):
    """Monthly cost per category per city. One row per (city, category, measured_date)."""

    __tablename__ = "cost_breakdowns"
    __table_args__ = (
        Index("ix_cost_city_cat_date", "city_id", "category", "measured_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False, index=True)
    category = Column(String(60), nullable=False, index=True)   # rent | groceries | transport | dining | coworking | utilities
    amount_usd = Column(Float, nullable=False)
    notes = Column(Text, nullable=True)
    measured_date = Column(Date, nullable=False, index=True)
    source = Column(String(80), nullable=True)
    extra_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class InternetMeasurement(Base):
    __tablename__ = "internet_measurements"
    __table_args__ = (
        Index("ix_internet_city_date", "city_id", "measured_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=False, index=True)
    download_mbps = Column(Float, nullable=True)
    upload_mbps = Column(Float, nullable=True)
    latency_ms = Column(Float, nullable=True)
    jitter_ms = Column(Float, nullable=True)
    measured_date = Column(Date, nullable=False, index=True)
    source = Column(String(80), nullable=True)
    extra_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class DataPoint(Base):
    """Generic time-series data for any (city|country, topic) combination."""

    __tablename__ = "data_points"
    __table_args__ = (
        Index("ix_dp_city_topic_date", "city_id", "topic_id", "measured_date"),
        Index("ix_dp_country_topic_date", "country_id", "topic_id", "measured_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    country_id = Column(Integer, ForeignKey("countries.id"), nullable=True, index=True)
    city_id = Column(Integer, ForeignKey("cities.id"), nullable=True, index=True)
    topic_id = Column(Integer, ForeignKey("topics.id"), nullable=True, index=True)
    metric = Column(String(80), nullable=False, index=True)
    value_num = Column(Float, nullable=True)
    value_text = Column(Text, nullable=True)
    measured_date = Column(Date, nullable=False, index=True)
    source = Column(String(120), nullable=True)
    extra_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────
# SEO research + logging
# ─────────────────────────────────────────────────────────────────────────

class KeywordOpportunity(Base):
    __tablename__ = "keyword_opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)
    keyword = Column(String(300), nullable=False, index=True)
    volume = Column(Integer, nullable=True)
    keyword_difficulty = Column(Float, nullable=True)
    cpc_usd = Column(Float, nullable=True)
    opportunity_score = Column(Float, nullable=True, index=True)
    intent = Column(String(40), nullable=True)        # informational | commercial | transactional | navigational
    serp_features_json = Column(JSON, nullable=True)
    target_url = Column(String(500), nullable=True)
    source = Column(String(40), nullable=True)        # ahrefs | manual
    notes = Column(Text, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow, index=True)


class DistributionLog(Base):
    __tablename__ = "distribution_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    channel = Column(String(40), nullable=False, index=True)   # google_indexing | indexnow
    url = Column(String(1000), nullable=False, index=True)
    entity_type = Column(String(40), nullable=True)
    entity_id = Column(Integer, nullable=True)
    success = Column(Boolean, nullable=False, default=False, index=True)
    response_code = Column(Integer, nullable=True)
    response_snippet = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)


class ScrapeLog(Base):
    __tablename__ = "scrape_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(_enum_values(SourceType), nullable=False)
    scrape_date = Column(Date, nullable=False)
    success = Column(Boolean, nullable=False)
    entries_found = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


# ─────────────────────────────────────────────────────────────────────────
# DB bootstrap
# ─────────────────────────────────────────────────────────────────────────

engine = create_engine(settings.database_url, echo=False)
SessionLocal = sessionmaker(bind=engine)
_init_lock = Lock()
_db_initialized = False


def init_db(*, force: bool = False):
    """Create tables once per process. Runs idempotent column/enum additions."""
    global _db_initialized
    if _db_initialized and not force:
        return
    with _init_lock:
        if _db_initialized and not force:
            return
        Base.metadata.create_all(engine)
        _ensure_columns()
        _ensure_enum_values()
        _db_initialized = True


def _ensure_columns() -> None:
    """Add columns introduced after a table was first created."""
    insp = sa_inspect(engine)
    dialect = engine.dialect.name

    blob_type = "BYTEA" if dialect == "postgresql" else "BLOB"
    json_type = "JSONB" if dialect == "postgresql" else "TEXT"

    additions: list[tuple[str, str, str]] = [
        ("blog_posts", "og_image_bytes", blob_type),
        ("blog_posts", "takeaways_json", json_type),
        ("landing_pages", "faq_json", json_type),
    ]

    for table_name, column_name, column_type in additions:
        if table_name not in insp.get_table_names():
            continue
        existing = {c["name"] for c in insp.get_columns(table_name)}
        if column_name in existing:
            continue
        with engine.begin() as conn:
            conn.execute(
                sa_text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}")
            )


_SOURCE_TYPE_ENUM_ADDITIONS: tuple[tuple[str, str], ...] = tuple(
    ("source_type", e.value) for e in SourceType
)


def _ensure_enum_values() -> None:
    if engine.dialect.name != "postgresql":
        return

    import logging
    log = logging.getLogger(__name__)

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for enum_name, value in _SOURCE_TYPE_ENUM_ADDITIONS:
            try:
                conn.execute(
                    sa_text(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{value}'")
                )
            except Exception as exc:
                log.warning(
                    "Could not add enum value %r to %s (continuing anyway): %s",
                    value, enum_name, exc,
                )
