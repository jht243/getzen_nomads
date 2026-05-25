from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    database_url: str = "sqlite:///./get_zen.db"
    storage_dir: Path = Path("./storage")
    output_dir: Path = Path("./output")

    log_level: str = "INFO"

    # Scraper
    scraper_timeout_seconds: int = 30
    scraper_max_retries: int = 3
    scraper_retry_delay_seconds: int = 60
    scraper_lookback_days: int = 30

    # LLM Analysis
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    openai_narrative_model: str = "gpt-4o-mini"
    analysis_min_relevance: int = 5
    report_lookback_days: int = 120
    llm_call_budget_per_run: int = 200
    llm_input_price_per_mtok: float = 2.50
    llm_output_price_per_mtok: float = 10.00

    # Premium model — evergreen landing content (country hubs, city guides)
    openai_premium_model: str = "gpt-5.2"
    llm_premium_input_price_per_mtok: float = 5.00
    llm_premium_output_price_per_mtok: float = 15.00

    # Newsletter
    newsletter_provider: str = "console"
    newsletter_from_email: str = "briefing@getzen.cash"
    newsletter_api_key: str = ""
    subscriber_list_path: str = "subscribers.json"
    seo_email_provider: str = "resend"
    seo_email_recipient: str = "<RECIPIENT_EMAIL>"
    seo_email_subject: str = "Get ZEN — Daily SEO"
    resend_api_key: str = ""

    # Buttondown
    buttondown_api_key: str = ""

    # Google reporting (GA4 + Search Console)
    google_reporting_sa_json: str = ""
    google_reporting_sa_file: str = ""
    google_reporting_ga4_property_id: str = ""
    google_reporting_gsc_site_url: str = ""
    google_reporting_output_dir: Path = Path("./output/google_reporting")
    google_reporting_ga_lookback_days: int = 30
    google_reporting_gsc_lookback_days: int = 90

    # Supabase Storage
    supabase_url: str = ""
    supabase_service_key: str = ""
    supabase_report_bucket: str = "reports"
    supabase_report_object_key: str = "report.html"

    # Server
    server_port: int = 8080

    # Admin
    admin_token: str = ""

    # SEO / canonical URL
    site_url: str = "https://www.getzen.cash"
    site_name: str = "Get ZEN"
    site_owner_org: str = "Get ZEN"
    site_locale: str = "en_US"

    @property
    def canonical_site_url(self) -> str:
        u = (self.site_url or "").strip().rstrip("/")
        if not u.startswith(("http://", "https://")):
            u = "https://" + u
        lower = u.lower()
        if not u or "onrender.com" in lower:
            return "https://www.getzen.cash"
        if lower in {"https://getzen.cash", "http://getzen.cash"}:
            return "https://www.getzen.cash"
        return u

    # Blog briefing generator
    blog_gen_budget_per_run: int = 10
    blog_gen_min_relevance: int = 5
    blog_gen_lookback_days: int = 14
    blog_gen_max_words: int = 900

    # Google News intake cap
    google_news_daily_cap: int = 40

    # Country rotation — how many countries to scrape per run
    country_rotation_per_run: int = 4

    # IndexNow
    indexnow_key: str = "0b2fff2a4cb56ba2c10382745f51cdd8"

    # Google Indexing API
    google_indexing_sa_json: str = ""
    google_indexing_sa_file: str = ""
    google_indexing_lookback_days: int = 7
    google_indexing_max_per_run: int = 50

    # Ahrefs
    ahrefs_api_key: str = ""
    ahrefs_project_id: str = ""

    # Landing-page generator: ground prompts with the OpenAI Responses API
    # + web_search_preview tool so the model fetches live citations before
    # writing (per the editorial brief — "Before writing, research the
    # topic using search tools"). On by default; flip via env var if you
    # want to skip live search for a faster/cheaper batch.
    landing_gen_web_search_enabled: bool = True

    # Reddit (nomad forums)
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "getzen/0.1"


settings = Settings()

settings.storage_dir.mkdir(parents=True, exist_ok=True)
settings.output_dir.mkdir(parents=True, exist_ok=True)
settings.google_reporting_output_dir.mkdir(parents=True, exist_ok=True)
