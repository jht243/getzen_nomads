"""Google Indexing API client.

Mints an OAuth2 token from a service-account JSON (env var or file path),
POSTs URL_UPDATED notifications. 200 URL/day default quota.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/indexing"]
_ENDPOINT = "https://indexing.googleapis.com/v3/urlNotifications:publish"


@dataclass
class IndexingResult:
    url: str
    success: bool
    status_code: int | None
    response_snippet: str


class GoogleIndexingClient:
    def __init__(self, credentials_info: dict):
        from google.oauth2 import service_account
        from google.auth.transport.requests import Request as GoogleAuthRequest

        self._creds = service_account.Credentials.from_service_account_info(
            credentials_info, scopes=_SCOPES
        )
        self._request = GoogleAuthRequest()

    def _ensure_token(self) -> str:
        if not self._creds.valid:
            self._creds.refresh(self._request)
        return self._creds.token

    def publish_url_updated(self, url: str) -> IndexingResult:
        try:
            token = self._ensure_token()
        except Exception as exc:
            logger.warning("indexing: token refresh failed for %s: %s", url, exc)
            return IndexingResult(url=url, success=False, status_code=None,
                                  response_snippet=f"token refresh failed: {exc}"[:500])

        try:
            resp = httpx.post(
                _ENDPOINT,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"url": url, "type": "URL_UPDATED"},
                timeout=15,
            )
        except Exception as exc:
            logger.warning("indexing: HTTP error for %s: %s", url, exc)
            return IndexingResult(url=url, success=False, status_code=None,
                                  response_snippet=f"http error: {exc}"[:500])

        snippet = (resp.text or "")[:500]
        if resp.status_code == 200:
            logger.info("indexing: 200 OK for %s", url)
            return IndexingResult(url=url, success=True, status_code=200, response_snippet=snippet)

        logger.warning("indexing: %d for %s -- %s", resp.status_code, url, snippet)
        return IndexingResult(url=url, success=False, status_code=resp.status_code, response_snippet=snippet)

    def publish_urls(self, urls: Iterable[str]) -> list[IndexingResult]:
        return [self.publish_url_updated(u) for u in urls]


_client: GoogleIndexingClient | None = None
_client_load_attempted = False


def _load_credentials_info() -> dict | None:
    raw = settings.google_indexing_sa_json.strip()
    if raw:
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.error("indexing: GOOGLE_INDEXING_SA_JSON is not valid JSON: %s", exc)
            return None

    file_path = settings.google_indexing_sa_file.strip()
    if file_path:
        path = Path(file_path).expanduser()
        if not path.exists():
            logger.error("indexing: GOOGLE_INDEXING_SA_FILE does not exist: %s", path)
            return None
        try:
            return json.loads(path.read_text())
        except Exception as exc:
            logger.error("indexing: failed to read %s: %s", path, exc)
            return None
    return None


def get_client() -> GoogleIndexingClient | None:
    global _client, _client_load_attempted
    if _client is not None:
        return _client
    if _client_load_attempted:
        return None
    _client_load_attempted = True
    info = _load_credentials_info()
    if info is None:
        logger.info("indexing: no credentials configured, skipping")
        return None
    try:
        _client = GoogleIndexingClient(info)
        return _client
    except Exception as exc:
        logger.error("indexing: failed to construct client: %s", exc, exc_info=True)
        return None


def is_enabled() -> bool:
    return bool(settings.google_indexing_sa_json.strip() or settings.google_indexing_sa_file.strip())
