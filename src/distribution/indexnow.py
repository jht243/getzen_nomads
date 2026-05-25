"""IndexNow client — single-POST notification to Bing, Yandex, Seznam, Naver, Mojeek.

Auth = domain ownership via a `/{key}.txt` file at the site root. The key
is not a secret — it's published on every API call.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_ENDPOINT = "https://api.indexnow.org/indexnow"


def _key() -> str:
    return (settings.indexnow_key or "").strip()


INDEXNOW_KEY = _key()


@dataclass
class IndexNowResult:
    success: bool
    status_code: int | None
    response_snippet: str
    submitted: int


def _host() -> str:
    base = settings.canonical_site_url.rstrip("/")
    if "://" in base:
        base = base.split("://", 1)[1]
    return base.split("/", 1)[0]


def _key_location() -> str:
    return f"{settings.canonical_site_url.rstrip('/')}/{_key()}.txt"


def submit_urls(urls: Iterable[str]) -> IndexNowResult:
    """POST up to 10,000 URLs in one shot."""
    key = _key()
    if not key:
        logger.info("indexnow: skipping — no INDEXNOW_KEY configured")
        return IndexNowResult(
            success=False, status_code=None,
            response_snippet="no INDEXNOW_KEY configured", submitted=0,
        )

    url_list = [u for u in urls if u]
    if not url_list:
        return IndexNowResult(success=True, status_code=None, response_snippet="no urls", submitted=0)

    payload = {
        "host": _host(),
        "key": key,
        "keyLocation": _key_location(),
        "urlList": url_list,
    }

    try:
        resp = httpx.post(
            _ENDPOINT,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=15,
        )
    except Exception as exc:
        logger.warning("indexnow: HTTP error: %s", exc)
        return IndexNowResult(
            success=False, status_code=None,
            response_snippet=f"http error: {exc}"[:500], submitted=0,
        )

    snippet = (resp.text or "")[:500]
    if resp.status_code in (200, 202):
        logger.info("indexnow: %d for %d URLs", resp.status_code, len(url_list))
        return IndexNowResult(
            success=True, status_code=resp.status_code,
            response_snippet=snippet or "ok", submitted=len(url_list),
        )

    logger.warning("indexnow: %d -- %s", resp.status_code, snippet)
    return IndexNowResult(
        success=False, status_code=resp.status_code,
        response_snippet=snippet, submitted=0,
    )
