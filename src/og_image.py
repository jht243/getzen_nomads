"""Per-page Open Graph card generator (1200x630 PNG).

Pure Pillow — no headless browser, no native deps beyond what Pillow needs.
One generator handles briefings and landing pages with different chip
labels and accent treatments.

Usage:
    png = render_briefing_card(title, category="Visa Policy",
                                published_date=date.today())
    png = render_landing_card(title, eyebrow="COLOMBIA · COUNTRY HUB")
"""
from __future__ import annotations

import io
import logging
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 1200, 630
LEFT_PANEL_W = 380

# Brand palette — matches templates/_base.html.j2
TEAL = (10, 77, 92)
TEAL_DEEP = (7, 57, 69)
TERRACOTTA = (211, 84, 0)
WHITE = (255, 255, 255)
INK = (15, 23, 42)
GRAY_500 = (100, 116, 139)
GRAY_300 = (203, 213, 225)
GRAY_100 = (241, 245, 249)


_SYSTEM_FONT_CHAIN = {
    "bold": [
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
        "/System/Library/Fonts/Avenir.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "regular": [
        "/System/Library/Fonts/Supplemental/Arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
    "serif_bold": [
        "/System/Library/Fonts/Supplemental/Georgia Bold.ttf",
        "/System/Library/Fonts/Supplemental/Times New Roman Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
    ],
}


@lru_cache(maxsize=64)
def _font(weight: str, size: int) -> ImageFont.ImageFont:
    for path in _SYSTEM_FONT_CHAIN.get(weight, []):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _measure(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _wrap_to_width(draw, text: str, font, max_width: int) -> list[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _measure(draw, candidate, font)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_headline(draw, text: str, *, max_width: int, max_lines: int,
                  initial_size: int, min_size: int) -> tuple:
    size = initial_size
    while size >= min_size:
        font = _font("serif_bold", size)
        lines = _wrap_to_width(draw, text, font, max_width)
        if len(lines) <= max_lines:
            return font, lines, size
        size -= 4
    font = _font("serif_bold", min_size)
    lines = _wrap_to_width(draw, text, font, max_width)
    head = lines[: max_lines - 1]
    tail = " ".join(lines[max_lines - 1:])
    while tail and _measure(draw, tail + "…", font)[0] > max_width:
        tail = tail.rsplit(" ", 1)[0] if " " in tail else tail[:-1]
    head.append((tail + "…") if tail else "…")
    return font, head, min_size


def _chip(draw, *, text: str, x: int, y: int,
          fill: tuple = TERRACOTTA, text_color: tuple = WHITE,
          pad_x: int = 16, pad_y: int = 8, font=None) -> tuple[int, int]:
    if font is None:
        font = _font("bold", 20)
    tw, th = _measure(draw, text, font)
    w, h = tw + 2 * pad_x, th + 2 * pad_y
    draw.rounded_rectangle((x, y, x + w, y + h), radius=5, fill=fill)
    draw.text((x + pad_x, y + pad_y - 2), text, font=font, fill=text_color)
    return w, h


def _render(*, title: str, eyebrow: str, chip_text: Optional[str] = None,
            chip_color: tuple = TERRACOTTA) -> bytes:
    img = Image.new("RGB", (WIDTH, HEIGHT), WHITE)
    draw = ImageDraw.Draw(img)

    # Left navy panel
    draw.rectangle((0, 0, LEFT_PANEL_W, HEIGHT), fill=TEAL)
    draw.rectangle((LEFT_PANEL_W - 6, 0, LEFT_PANEL_W, HEIGHT), fill=TERRACOTTA)

    # Brand mark — large "Z" with brand text below
    z_font = _font("serif_bold", 220)
    zw, zh = _measure(draw, "Z", z_font)
    z_x = (LEFT_PANEL_W - zw) // 2 - 12
    z_y = 110
    draw.text((z_x, z_y), "Z", font=z_font, fill=WHITE)

    brand_font = _font("bold", 30)
    brand_text = "GET ZEN"
    bw, _ = _measure(draw, brand_text, brand_font)
    draw.text(((LEFT_PANEL_W - bw) // 2, z_y + zh + 24), brand_text, font=brand_font, fill=WHITE)

    tagline_font = _font("regular", 18)
    tagline = "Digital nomad intel"
    tw, _ = _measure(draw, tagline, tagline_font)
    draw.text(((LEFT_PANEL_W - tw) // 2, z_y + zh + 70), tagline, font=tagline_font, fill=GRAY_300)

    # Right content panel
    content_x = LEFT_PANEL_W + 60
    content_w = WIDTH - content_x - 60
    cursor_y = 70

    if eyebrow:
        eyebrow_font = _font("bold", 18)
        draw.text((content_x, cursor_y), eyebrow.upper(), font=eyebrow_font, fill=GRAY_500)
        cursor_y += 36

    if chip_text:
        _chip(draw, text=chip_text.upper(), x=content_x, y=cursor_y,
              fill=chip_color, font=_font("bold", 18))
        cursor_y += 56

    headline_font, headline_lines, _ = _fit_headline(
        draw, title,
        max_width=content_w,
        max_lines=4,
        initial_size=58,
        min_size=34,
    )
    line_h = int(_measure(draw, "Ay", headline_font)[1] * 1.18)
    for line in headline_lines:
        draw.text((content_x, cursor_y), line, font=headline_font, fill=INK)
        cursor_y += line_h

    # Footer URL line, flush right
    url_font = _font("regular", 18)
    url_text = "getzen.cash"
    uw, _ = _measure(draw, url_text, url_font)
    draw.text((WIDTH - 60 - uw, HEIGHT - 50), url_text, font=url_font, fill=GRAY_500)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── Public API ────────────────────────────────────────────────────────────
def render_briefing_card(
    *,
    title: str,
    category: Optional[str] = None,
    published_date: Optional[date] = None,
) -> bytes:
    eyebrow = "DAILY BRIEFING"
    if published_date:
        eyebrow = f"BRIEFING · {published_date.strftime('%b %d, %Y').upper()}"
    return _render(title=title, eyebrow=eyebrow, chip_text=category)


def render_landing_card(
    *,
    title: str,
    eyebrow: Optional[str] = None,
    chip: Optional[str] = None,
) -> bytes:
    return _render(title=title, eyebrow=eyebrow or "GET ZEN", chip_text=chip,
                   chip_color=TEAL)


def render_default_card() -> bytes:
    """Fallback card used when a page has no per-page OG image."""
    return _render(
        title="Digital nomad intelligence.",
        eyebrow="GET ZEN",
        chip_text="GETZEN.CASH",
        chip_color=TEAL,
    )
