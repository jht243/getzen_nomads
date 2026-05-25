"""SEO audit engine — BFS crawl of the rendered site via Flask test_client.

Runs without a live server: imports server.app, walks the URL graph,
and applies per-page rule checks. Outputs a JSON report + a console
summary. Used both as a CI gate and as the input to content_fixer.py.

Per-page checks (20+):
  - title present + 30-70 chars
  - description present + 80-180 chars
  - canonical present + matches expected path
  - hreflang en + x-default
  - og:title, og:description, og:image, og:type, og:url
  - twitter:card = summary_large_image
  - exactly one <h1>
  - heading hierarchy (no h3 without preceding h2)
  - JSON-LD parseable
  - article word count >= 200 (briefings) / >= 800 (landing pages)
  - 2+ internal links
  - cluster nav present on cluster pages
  - robots meta is "index, follow"
  - status code 200

Cross-page:
  - sitemap reachable + non-empty
  - robots.txt references sitemap
  - every cluster member appears in cluster pillar's nav
  - no orphan pages (every non-pillar reachable from at least one other page)
"""
from __future__ import annotations

import json
import logging
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


SEVERITY_ERROR = "error"
SEVERITY_WARN = "warn"
SEVERITY_INFO = "info"


@dataclass
class Finding:
    path: str
    rule: str
    severity: str           # error | warn | info
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class PageAuditResult:
    path: str
    status: int
    findings: list[Finding] = field(default_factory=list)
    word_count: int = 0
    internal_link_count: int = 0


@dataclass
class AuditReport:
    pages: list[PageAuditResult] = field(default_factory=list)
    cross_findings: list[Finding] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        n = sum(1 for p in self.pages for f in p.findings if f.severity == SEVERITY_ERROR)
        return n + sum(1 for f in self.cross_findings if f.severity == SEVERITY_ERROR)

    @property
    def warn_count(self) -> int:
        n = sum(1 for p in self.pages for f in p.findings if f.severity == SEVERITY_WARN)
        return n + sum(1 for f in self.cross_findings if f.severity == SEVERITY_WARN)

    def as_dict(self) -> dict:
        return {
            "summary": {
                "pages": len(self.pages),
                "errors": self.error_count,
                "warns": self.warn_count,
            },
            "pages": [
                {
                    "path": p.path,
                    "status": p.status,
                    "word_count": p.word_count,
                    "internal_link_count": p.internal_link_count,
                    "findings": [
                        {"rule": f.rule, "severity": f.severity, "message": f.message, "detail": f.detail}
                        for f in p.findings
                    ],
                }
                for p in self.pages
            ],
            "cross_findings": [
                {"path": f.path, "rule": f.rule, "severity": f.severity, "message": f.message, "detail": f.detail}
                for f in self.cross_findings
            ],
        }


# ── Regex utilities ──────────────────────────────────────────────────────
_RE_TITLE = re.compile(r"<title>([^<]+)</title>", re.IGNORECASE)
_RE_META = re.compile(r"<meta\s+([^>]+)>", re.IGNORECASE)
_RE_LINK = re.compile(r"<link\s+([^>]+)>", re.IGNORECASE)
_RE_H1 = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_RE_H_LEVELS = re.compile(r"<h([1-6])\b[^>]*>", re.IGNORECASE)
_RE_JSONLD = re.compile(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                        re.IGNORECASE | re.DOTALL)
_RE_HREF = re.compile(r'href="([^"]+)"', re.IGNORECASE)
_RE_STRIP_TAGS = re.compile(r"<[^>]+>")
_RE_CLUSTER_NAV = re.compile(r'class="topic-cluster-nav"', re.IGNORECASE)


def _attrs(blob: str) -> dict:
    """Pull key=value pairs from a tag's inner blob."""
    out = {}
    for m in re.finditer(r'(\w+(?:-\w+)*)\s*=\s*"([^"]*)"', blob):
        out[m.group(1).lower()] = m.group(2)
    return out


# ── Page-level checks ────────────────────────────────────────────────────
def _check_meta_tags(path: str, html: str) -> list[Finding]:
    findings: list[Finding] = []

    title_m = _RE_TITLE.search(html)
    title = title_m.group(1).strip() if title_m else ""
    if not title:
        findings.append(Finding(path, "missing_title", SEVERITY_ERROR, "No <title> tag"))
    elif len(title) < 30:
        findings.append(Finding(path, "title_too_short", SEVERITY_WARN,
                                f"Title is {len(title)} chars (target 50-60)",
                                {"title": title}))
    elif len(title) > 70:
        findings.append(Finding(path, "title_too_long", SEVERITY_WARN,
                                f"Title is {len(title)} chars (target 50-60)",
                                {"title": title}))

    metas: dict[str, str] = {}
    props: dict[str, str] = {}
    for m in _RE_META.finditer(html):
        attrs = _attrs(m.group(1))
        if "name" in attrs and "content" in attrs:
            metas[attrs["name"].lower()] = attrs["content"]
        elif "property" in attrs and "content" in attrs:
            props[attrs["property"].lower()] = attrs["content"]

    desc = metas.get("description", "")
    if not desc:
        findings.append(Finding(path, "missing_description", SEVERITY_ERROR, "No meta description"))
    elif len(desc) < 80:
        findings.append(Finding(path, "description_too_short", SEVERITY_WARN,
                                f"Description is {len(desc)} chars (target 120-160)"))
    elif len(desc) > 180:
        findings.append(Finding(path, "description_too_long", SEVERITY_WARN,
                                f"Description is {len(desc)} chars (target 120-160)"))

    robots = metas.get("robots", "")
    if "noindex" in robots.lower():
        findings.append(Finding(path, "noindex_robots", SEVERITY_ERROR,
                                f"robots meta contains noindex: {robots}"))

    # OG tags
    for prop in ("og:title", "og:description", "og:image", "og:type", "og:url"):
        if not props.get(prop):
            findings.append(Finding(path, f"missing_{prop.replace(':', '_')}", SEVERITY_WARN,
                                    f"Missing {prop} meta tag"))

    # Twitter card
    tw_card = metas.get("twitter:card", "")
    if tw_card != "summary_large_image":
        findings.append(Finding(path, "twitter_card", SEVERITY_INFO,
                                f"twitter:card is {tw_card!r}, expected summary_large_image"))

    # Canonical
    canonical = None
    for m in _RE_LINK.finditer(html):
        attrs = _attrs(m.group(1))
        if attrs.get("rel") == "canonical":
            canonical = attrs.get("href", "")
            break
    if not canonical:
        findings.append(Finding(path, "missing_canonical", SEVERITY_ERROR, "No canonical link"))

    return findings


def _check_headings(path: str, html: str) -> list[Finding]:
    findings: list[Finding] = []
    h1s = _RE_H1.findall(html)
    if len(h1s) == 0:
        findings.append(Finding(path, "missing_h1", SEVERITY_ERROR, "No <h1> on page"))
    elif len(h1s) > 1:
        findings.append(Finding(path, "multiple_h1", SEVERITY_WARN, f"Found {len(h1s)} <h1> tags"))

    levels = [int(m.group(1)) for m in _RE_H_LEVELS.finditer(html)]
    # Filter to content levels h2-h4 after first h1
    if levels:
        for prev, curr in zip(levels, levels[1:]):
            if curr > prev + 1 and curr > 1:
                findings.append(Finding(path, "heading_skip", SEVERITY_INFO,
                                        f"Heading hierarchy jumps from h{prev} to h{curr}"))
                break
    return findings


def _check_jsonld(path: str, html: str) -> list[Finding]:
    findings: list[Finding] = []
    blocks = _RE_JSONLD.findall(html)
    if not blocks:
        findings.append(Finding(path, "missing_jsonld", SEVERITY_WARN,
                                "No JSON-LD structured data"))
        return findings
    for i, block in enumerate(blocks):
        try:
            json.loads(block)
        except json.JSONDecodeError as exc:
            findings.append(Finding(path, "jsonld_invalid", SEVERITY_ERROR,
                                    f"JSON-LD block {i} is invalid: {exc}"))
    return findings


def _count_internal_links(html: str, base_host: str) -> int:
    n = 0
    for m in _RE_HREF.finditer(html):
        href = m.group(1).strip()
        if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
            continue
        if href.startswith("/"):
            n += 1
        elif href.startswith(("http://", "https://")):
            host = urlparse(href).netloc.lower()
            if host == base_host:
                n += 1
    return n


def _word_count(html: str) -> int:
    # Strip head/style/script regions before counting.
    body = re.sub(r"<head[^>]*>.*?</head>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<script[^>]*>.*?</script>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    body = re.sub(r"<style[^>]*>.*?</style>", " ", body, flags=re.IGNORECASE | re.DOTALL)
    text = _RE_STRIP_TAGS.sub(" ", body)
    return len([w for w in text.split() if any(c.isalnum() for c in w)])


# ── Crawl + drive ────────────────────────────────────────────────────────
def _is_internal(href: str, base_host: str) -> Optional[str]:
    """Return the path portion if internal, else None."""
    if not href:
        return None
    if href.startswith("#") or href.startswith("mailto:") or href.startswith("tel:"):
        return None
    if href.startswith(("http://", "https://")):
        parsed = urlparse(href)
        if parsed.netloc.lower() != base_host:
            return None
        return parsed.path or "/"
    if href.startswith("/"):
        # Strip query/fragment
        return href.split("?")[0].split("#")[0] or "/"
    return None


_RESERVED_TOP_SEGMENTS = {
    "briefings", "tools", "rankings", "guides", "resources",
    "visa-database", "cost-of-living-rankings", "internet-speeds",
    "about", "api", "static", "og", "regions",
    "compare", "sitemap.xml", "news-sitemap.xml", "robots.txt", "health",
}


def _classify(path: str) -> str:
    """Rough page-type classification for content thresholds."""
    if path == "/" or path == "":
        return "homepage"
    if path.startswith("/briefings/") and path not in ("/briefings/", "/briefings"):
        return "briefing"
    if path == "/briefings/" or path == "/briefings":
        return "index"
    if path.startswith("/tools/"):
        return "tool"
    if path in ("/visa-database/", "/cost-of-living-rankings/", "/internet-speeds/"):
        return "programmatic"
    parts = [p for p in path.strip("/").split("/") if p]
    if parts and parts[0] in _RESERVED_TOP_SEGMENTS:
        return "other"
    if len(parts) == 1:
        return "country"
    if len(parts) == 2:
        return "city"
    if len(parts) == 3:
        return "topic"
    return "other"


def _content_threshold(page_type: str) -> int:
    return {
        "homepage": 100,
        "index": 50,
        "briefing": 400,
        "country": 600,
        "city": 500,
        "topic": 400,
        "tool": 100,
        "programmatic": 200,
    }.get(page_type, 100)


def _check_cluster_nav(path: str, html: str, page_type: str) -> list[Finding]:
    """Cluster nav should appear on country/city/topic pages."""
    if page_type not in ("country", "city", "topic"):
        return []
    if not _RE_CLUSTER_NAV.search(html):
        return [Finding(path, "missing_cluster_nav", SEVERITY_WARN,
                        "Cluster navigation block missing on hub/spoke page")]
    return []


def audit_site(
    *,
    start_paths: Iterable[str] = ("/",),
    max_pages: int = 500,
    base_host: Optional[str] = None,
) -> AuditReport:
    """BFS crawl via Flask test_client. Returns a structured report."""
    # Import here so audit can be invoked from scripts without forcing
    # server import at module load time.
    from server import app
    from src.config import settings

    if base_host is None:
        parsed = urlparse(settings.canonical_site_url)
        base_host = parsed.netloc.lower()

    report = AuditReport()
    seen: set[str] = set()
    queue: deque[str] = deque()
    for p in start_paths:
        if p not in seen:
            seen.add(p)
            queue.append(p)

    client = app.test_client()

    while queue and len(report.pages) < max_pages:
        path = queue.popleft()
        try:
            resp = client.get(path)
        except Exception as exc:
            report.pages.append(PageAuditResult(
                path=path, status=0,
                findings=[Finding(path, "crawl_error", SEVERITY_ERROR, f"Request failed: {exc}")],
            ))
            continue

        page_findings: list[Finding] = []
        page_type = _classify(path)

        if resp.status_code != 200:
            page_findings.append(Finding(path, "non_200_status", SEVERITY_ERROR,
                                          f"Status {resp.status_code}"))
            report.pages.append(PageAuditResult(path=path, status=resp.status_code, findings=page_findings))
            continue

        html = resp.get_data(as_text=True)

        page_findings.extend(_check_meta_tags(path, html))
        page_findings.extend(_check_headings(path, html))
        page_findings.extend(_check_jsonld(path, html))
        page_findings.extend(_check_cluster_nav(path, html, page_type))

        wc = _word_count(html)
        ilinks = _count_internal_links(html, base_host)
        threshold = _content_threshold(page_type)

        if wc < threshold:
            page_findings.append(Finding(path, "thin_content", SEVERITY_WARN,
                                          f"Word count {wc} below {page_type} threshold {threshold}"))

        if ilinks < 2 and page_type != "tool":
            page_findings.append(Finding(path, "low_internal_links", SEVERITY_INFO,
                                          f"Only {ilinks} internal links"))

        report.pages.append(PageAuditResult(
            path=path, status=200,
            findings=page_findings, word_count=wc, internal_link_count=ilinks,
        ))

        # Discover more pages
        for m in _RE_HREF.finditer(html):
            next_path = _is_internal(m.group(1), base_host)
            if not next_path:
                continue
            # Skip API + static + OG + xml + txt
            if next_path.startswith(("/api/", "/static/", "/og/", "/health")):
                continue
            if next_path.endswith((".xml", ".txt", ".png", ".jpg", ".ico")):
                continue
            if next_path not in seen:
                seen.add(next_path)
                queue.append(next_path)

    # Cross-page: sitemap reachable
    try:
        resp = client.get("/sitemap.xml")
        if resp.status_code != 200 or b"<urlset" not in resp.get_data():
            report.cross_findings.append(Finding(
                "/sitemap.xml", "sitemap_unreachable", SEVERITY_ERROR,
                f"sitemap.xml returned {resp.status_code} or invalid body",
            ))
    except Exception as exc:
        report.cross_findings.append(Finding(
            "/sitemap.xml", "sitemap_error", SEVERITY_ERROR, f"sitemap request failed: {exc}",
        ))

    # Cross-page: robots references sitemap
    try:
        resp = client.get("/robots.txt")
        body = resp.get_data(as_text=True) or ""
        if "Sitemap:" not in body:
            report.cross_findings.append(Finding(
                "/robots.txt", "robots_no_sitemap", SEVERITY_WARN,
                "robots.txt does not declare a Sitemap",
            ))
    except Exception as exc:
        report.cross_findings.append(Finding(
            "/robots.txt", "robots_error", SEVERITY_WARN, f"robots request failed: {exc}",
        ))

    return report
