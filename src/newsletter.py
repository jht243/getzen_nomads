"""Newsletter sending + subscriber management.

Pluggable providers (settings.newsletter_provider):
  - console    — log to stdout, default for dev
  - resend     — Resend HTTPS API
  - buttondown — Buttondown HTTPS API (subscribe + broadcast)

Subscriber management goes through `add_subscriber(email)` regardless of
provider, so the /api/subscribe endpoint stays provider-agnostic.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)


@dataclass
class SendResult:
    provider: str
    success: bool
    response_snippet: str = ""
    error: str = ""


# ── Subscriber storage ────────────────────────────────────────────────────
def _subscriber_path() -> Path:
    return Path(settings.subscriber_list_path)


def _load_subscribers() -> list[dict]:
    p = _subscriber_path()
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_subscribers(rows: list[dict]) -> None:
    _subscriber_path().write_text(json.dumps(rows, indent=2))


def add_subscriber(email: str, *, source: str = "web", ip: str | None = None) -> bool:
    """Local-file subscriber list. Returns True if new, False if duplicate."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return False
    rows = _load_subscribers()
    for row in rows:
        if row.get("email") == email:
            return False
    rows.append({"email": email, "source": source, "ip": ip})
    _save_subscribers(rows)

    # Mirror into Buttondown if configured. Failure here doesn't block
    # the local save — we still capture the lead and can re-sync later.
    if settings.buttondown_api_key:
        try:
            httpx.post(
                "https://api.buttondown.email/v1/subscribers",
                headers={
                    "Authorization": f"Token {settings.buttondown_api_key}",
                    "Content-Type": "application/json",
                },
                json={"email": email, "tags": ["getzen", source]},
                timeout=10,
            )
        except Exception as exc:
            logger.warning("buttondown subscribe failed for %s: %s", email, exc)
    return True


# ── Broadcast sending ─────────────────────────────────────────────────────
def send_email(
    *,
    subject: str,
    html_body: str,
    plain_body: Optional[str] = None,
    to_email: Optional[str] = None,
    from_email: Optional[str] = None,
    reply_to: Optional[str] = None,
) -> SendResult:
    """Send a single email via the configured provider."""
    provider = (settings.newsletter_provider or "console").lower()
    from_email = from_email or settings.newsletter_from_email
    to_email = to_email or settings.seo_email_recipient

    if provider == "console":
        logger.info("[console-email] to=%s from=%s subject=%s", to_email, from_email, subject)
        logger.info("[console-email] body (first 500 chars): %s", (plain_body or html_body or "")[:500])
        return SendResult(provider="console", success=True, response_snippet="logged to stdout")

    if provider == "resend":
        api_key = settings.resend_api_key
        if not api_key:
            return SendResult(provider="resend", success=False, error="RESEND_API_KEY not set")
        try:
            resp = httpx.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": from_email,
                    "to": [to_email] if to_email else [],
                    "subject": subject,
                    "html": html_body,
                    "text": plain_body or "",
                    "reply_to": reply_to,
                },
                timeout=15,
            )
            return SendResult(
                provider="resend",
                success=resp.status_code < 300,
                response_snippet=(resp.text or "")[:300],
            )
        except Exception as exc:
            return SendResult(provider="resend", success=False, error=str(exc))

    if provider == "buttondown":
        api_key = settings.buttondown_api_key
        if not api_key:
            return SendResult(provider="buttondown", success=False, error="BUTTONDOWN_API_KEY not set")
        try:
            resp = httpx.post(
                "https://api.buttondown.email/v1/emails",
                headers={"Authorization": f"Token {api_key}", "Content-Type": "application/json"},
                json={"subject": subject, "body": html_body, "status": "draft"},
                timeout=15,
            )
            return SendResult(
                provider="buttondown",
                success=resp.status_code < 300,
                response_snippet=(resp.text or "")[:300],
            )
        except Exception as exc:
            return SendResult(provider="buttondown", success=False, error=str(exc))

    return SendResult(provider=provider, success=False, error=f"Unknown provider: {provider}")
