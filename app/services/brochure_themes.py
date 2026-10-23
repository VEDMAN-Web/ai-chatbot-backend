"""
User-side brochure visual themes (Jinja2 + WeasyPrint).

Seven palettes/layouts modeled on Pdf_Templet Varsovia_Estate_Dariusz_Tajlandia 1–7.
Theme selection is random per request unless an explicit template id is forced.
"""

from __future__ import annotations

import logging
import os
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

THEME_IDS: List[int] = [1, 2, 3, 4, 5, 6, 7]

# Last theme used per session/property key — avoid immediate repeats when possible.
_LAST_THEME: Dict[str, int] = {}


@dataclass(frozen=True)
class ThemeSpec:
    id: int
    name: str
    shape: str  # rect | rounded | arch
    layout: str  # band | sidebar | framed | pastel | arch | cards | split
    palette: Dict[str, str] = field(default_factory=dict)
    css_file: str = ""

    def css_vars(self) -> Dict[str, str]:
        return dict(self.palette)


THEMES: Dict[int, ThemeSpec] = {
    1: ThemeSpec(
        id=1,
        name="Navy Red",
        shape="rect",
        layout="band",
        css_file="theme_1.css",
        palette={
            "bg": "#0B1D36",
            "bg_alt": "#FFFFFF",
            "paper": "#FFFFFF",
            "ink": "#0B1D36",
            "muted": "#5A6575",
            "accent": "#C41E3A",
            "accent_text": "#FFFFFF",
            "chip": "#C41E3A",
            "bar": "#0B1D36",
            "card": "#F4F6F8",
            "price_bg": "#0B1D36",
            "price_fg": "#FFFFFF",
            "notice": "#0B1D36",
            "cream": "#F7F4EE",
        },
    ),
    2: ThemeSpec(
        id=2,
        name="Black Gold",
        shape="rect",
        layout="sidebar",
        css_file="theme_2.css",
        palette={
            "bg": "#1A1A1A",
            "bg_alt": "#F5F0E6",
            "paper": "#F5F0E6",
            "ink": "#1A1A1A",
            "muted": "#6A6358",
            "accent": "#C9A227",
            "accent_text": "#1A1A1A",
            "chip": "#C9A227",
            "bar": "#1A1A1A",
            "card": "#EDE6D6",
            "price_bg": "#1A1A1A",
            "price_fg": "#C9A227",
            "notice": "#1A1A1A",
            "cream": "#F5F0E6",
        },
    ),
    3: ThemeSpec(
        id=3,
        name="Electric Blue",
        shape="rect",
        layout="framed",
        css_file="theme_3.css",
        palette={
            "bg": "#FFFFFF",
            "bg_alt": "#FFFFFF",
            "paper": "#FFFFFF",
            "ink": "#0A1628",
            "muted": "#5A6A7A",
            "accent": "#0066FF",
            "accent_text": "#FFFFFF",
            "chip": "#0066FF",
            "bar": "#0066FF",
            "card": "#E8F0FF",
            "price_bg": "#0066FF",
            "price_fg": "#FFFFFF",
            "notice": "#0066FF",
            "cream": "#F5F8FC",
        },
    ),
    4: ThemeSpec(
        id=4,
        name="Soft Pastel",
        shape="rounded",
        layout="pastel",
        css_file="theme_4.css",
        palette={
            "bg": "#F7F1E8",
            "bg_alt": "#F7F1E8",
            "paper": "#F7F1E8",
            "ink": "#3D3429",
            "muted": "#7A6F63",
            "accent": "#E8A0BF",
            "accent_text": "#3D3429",
            "chip": "#E8A0BF",
            "bar": "#C5B4E3",
            "card": "#FFFFFF",
            "price_bg": "#E8A0BF",
            "price_fg": "#3D3429",
            "notice": "#C5B4E3",
            "cream": "#F7F1E8",
            "card_green": "#A8D5BA",
            "card_blue": "#A0C4E8",
            "card_lilac": "#C5B4E3",
        },
    ),
    5: ThemeSpec(
        id=5,
        name="Forest Gold",
        shape="arch",
        layout="arch",
        css_file="theme_5.css",
        palette={
            "bg": "#1B4332",
            "bg_alt": "#F5F0E6",
            "paper": "#F5F0E6",
            "ink": "#1B4332",
            "muted": "#5C6B5A",
            "accent": "#D4AF37",
            "accent_text": "#1B4332",
            "chip": "#D4AF37",
            "bar": "#1B4332",
            "card": "#E8E2D4",
            "price_bg": "#1B4332",
            "price_fg": "#D4AF37",
            "notice": "#1B4332",
            "cream": "#F5F0E6",
        },
    ),
    6: ThemeSpec(
        id=6,
        name="Green Brown Cards",
        shape="rounded",
        layout="cards",
        css_file="theme_6.css",
        palette={
            "bg": "#E8F0E3",
            "bg_alt": "#E8F0E3",
            "paper": "#E8F0E3",
            "ink": "#3D2914",
            "muted": "#6B5A48",
            "accent": "#C41E3A",
            "accent_text": "#FFFFFF",
            "chip": "#C41E3A",
            "bar": "#3D2914",
            "card": "#3D2914",
            "price_bg": "#2D5A3D",
            "price_fg": "#FFFFFF",
            "notice": "#3D2914",
            "cream": "#E8F0E3",
            "green": "#2D5A3D",
        },
    ),
    7: ThemeSpec(
        id=7,
        name="Green Brown Split",
        shape="rect",
        layout="split",
        css_file="theme_7.css",
        palette={
            "bg": "#E8F0E3",
            "bg_alt": "#FFFFFF",
            "paper": "#FFFFFF",
            "ink": "#3D2914",
            "muted": "#6B5A48",
            "accent": "#C41E3A",
            "accent_text": "#FFFFFF",
            "chip": "#C41E3A",
            "bar": "#C41E3A",
            "card": "#3D2914",
            "price_bg": "#2D5A3D",
            "price_fg": "#FFFFFF",
            "notice": "#3D2914",
            "cream": "#E8F0E3",
            "green": "#2D5A3D",
        },
    ),
}


def get_theme(theme_id: int) -> ThemeSpec:
    if theme_id not in THEMES:
        raise KeyError(f"Unknown brochure theme id: {theme_id}")
    return THEMES[theme_id]


def _coerce_theme_id(value: Optional[object]) -> Optional[int]:
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    if n in THEMES:
        return n
    return None


def _forced_theme_from_env() -> Optional[int]:
    raw = os.environ.get("BROCHURE_FORCE_THEME")
    if raw is None or str(raw).strip() == "":
        try:
            from app.core.config import settings

            raw = getattr(settings, "BROCHURE_FORCE_THEME", None)
        except Exception:
            raw = None
    return _coerce_theme_id(raw)


def pick_theme(
    requested: Optional[int] = None,
    exclude: Optional[int] = None,
    *,
    session_key: Optional[str] = None,
) -> int:
    """
    Choose a theme id in 1..7.

    Priority: explicit requested (query/arg) > BROCHURE_FORCE_THEME > random.
    When possible, avoid repeating the last theme for the same session_key.
    """
    forced = _coerce_theme_id(requested)
    if forced is None:
        forced = _forced_theme_from_env()
    if forced is not None:
        chosen = forced
    else:
        last = None
        if session_key:
            last = _LAST_THEME.get(session_key)
        avoid = exclude if exclude is not None else last
        pool = [t for t in THEME_IDS if t != avoid] or list(THEME_IDS)
        chosen = random.choice(pool)

    if session_key:
        _LAST_THEME[session_key] = chosen
    return chosen


def remember_theme(session_key: Optional[str], theme_id: int) -> None:
    if session_key and theme_id in THEMES:
        _LAST_THEME[session_key] = theme_id


def clear_theme_memory() -> None:
    """Test helper."""
    _LAST_THEME.clear()


def fallback_theme_order(failed_id: Optional[int] = None) -> List[int]:
    """Ordered theme ids to try after a render failure (then legacy base)."""
    order = [t for t in THEME_IDS if t != failed_id]
    random.shuffle(order)
    if failed_id and failed_id in THEME_IDS:
        # Prefer a different theme first
        pass
    return order
