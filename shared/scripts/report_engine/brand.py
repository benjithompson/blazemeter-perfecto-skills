"""The swappable **Brand Config** the Branded Report Template reads.

Branding (colors, fonts, logo) lives here, not in the template. v1 ships an
**approximated** BlazeMeter brand; switching to official assets later is a config
+ logo swap, not a template change (ADR-0009). Fonts are a CSS font *stack* of
system fonts — no web-font CDN — so the Report stays fully offline.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

ASSETS = Path(__file__).resolve().parent / "assets"
DEFAULT_BRAND_JSON = ASSETS / "brand.default.json"
DEFAULT_LOGO_SVG = ASSETS / "logo.svg"

#: Colour keys the template expects; each becomes a CSS custom property.
_COLOR_KEYS = (
    "primary",
    "primary_dark",
    "accent",
    "bg",
    "surface",
    "border",
    "text",
    "muted",
    "good",
    "warn",
    "bad",
)


@dataclass(frozen=True)
class BrandConfig:
    """Resolved brand values. ``logo_svg`` is inline SVG markup (so the file stays
    self-contained); ``font_stack`` is a CSS ``font-family`` value."""

    name: str
    primary: str
    primary_dark: str
    accent: str
    bg: str
    surface: str
    border: str
    text: str
    muted: str
    good: str
    warn: str
    bad: str
    font_stack: str
    logo_svg: str

    def css_variables(self) -> str:
        """Render the brand as CSS custom properties for the template's ``:root``."""
        lines = ["  --brand-%s: %s;" % (k.replace("_", "-"), getattr(self, k)) for k in _COLOR_KEYS]
        lines.append("  --brand-font: %s;" % self.font_stack)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def brand_from_dict(d: Mapping[str, Any], *, logo_svg: str | None = None) -> BrandConfig:
    """Build a ``BrandConfig`` from a dict. ``logo_svg`` may be supplied inline, or
    given in the dict as ``logo_svg`` (inline) — at least one must resolve."""
    if not isinstance(d, Mapping):
        raise ValueError("brand config must be an object")
    required = ("name", "font_stack", *_COLOR_KEYS)
    missing = [k for k in required if not str(d.get(k, "")).strip()]
    if missing:
        raise ValueError("brand config missing: %s" % ", ".join(missing))
    logo = logo_svg if logo_svg is not None else d.get("logo_svg")
    if not logo or "<svg" not in str(logo):
        raise ValueError("brand config requires inline SVG 'logo_svg'")
    return BrandConfig(
        name=str(d["name"]),
        primary=str(d["primary"]),
        primary_dark=str(d["primary_dark"]),
        accent=str(d["accent"]),
        bg=str(d["bg"]),
        surface=str(d["surface"]),
        border=str(d["border"]),
        text=str(d["text"]),
        muted=str(d["muted"]),
        good=str(d["good"]),
        warn=str(d["warn"]),
        bad=str(d["bad"]),
        font_stack=str(d["font_stack"]),
        logo_svg=str(logo),
    )


def load_brand(config_path: str | Path, *, logo_path: str | Path | None = None) -> BrandConfig:
    """Load a Brand Config JSON file (and an optional sibling logo SVG)."""
    config_path = Path(config_path)
    data = json.loads(config_path.read_text(encoding="utf-8"))
    logo_svg = None
    if logo_path is not None:
        logo_svg = Path(logo_path).read_text(encoding="utf-8")
    elif "logo_svg" not in data:
        sibling = config_path.parent / "logo.svg"
        if sibling.is_file():
            logo_svg = sibling.read_text(encoding="utf-8")
    return brand_from_dict(data, logo_svg=logo_svg)


def load_default_brand() -> BrandConfig:
    """The shipped, approximated-BlazeMeter Brand Config."""
    return load_brand(DEFAULT_BRAND_JSON, logo_path=DEFAULT_LOGO_SVG)
