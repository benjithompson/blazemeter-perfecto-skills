"""BlazeMeter branded Report engine.

Separates retrieval from rendering with a normalized **Report data model** in
between (the primary test seam): a retrieval step (e.g. the cross-run report
skill) produces a ``ReportModel``; the pure ``render_report`` renderer turns it
into a single-file, self-contained, branded HTML Report.

See ``docs/adr/0005-reporting-first-class-branded-template.md`` and
``docs/adr/0009-report-engine-self-contained-html-swappable-brand.md``.
"""

from __future__ import annotations

from .brand import BrandConfig, brand_from_dict, load_default_brand
from .model import ReportModel, ReportModelError, model_from_dict
from .renderer import render_report

__all__ = [
    "BrandConfig",
    "ReportModel",
    "ReportModelError",
    "brand_from_dict",
    "load_default_brand",
    "model_from_dict",
    "render_report",
]
