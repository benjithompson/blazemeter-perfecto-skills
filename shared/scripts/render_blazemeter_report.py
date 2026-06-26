#!/usr/bin/env python3
"""Render a BlazeMeter Report data model (JSON) into a self-contained HTML Report.

Thin CLI over the ``report_engine`` package (the deterministic renderer + Branded
Report Template). A retrieval skill writes a Report data model to JSON; this turns
it into a single offline HTML file under a configurable output dir
(default ``./blazemeter-reports/``).

Stdlib-only, so CI's ``--help`` smoke needs nothing installed; ``--help`` does no
I/O beyond argparse. Credentials are never read or embedded — the renderer only
ever sees the data model.

Usage:
    python render_blazemeter_report.py --model report.json
    python render_blazemeter_report.py --model report.json --out ./out --brand my-brand.json
    python render_blazemeter_report.py --model report.json --stdout > report.html
    python render_blazemeter_report.py --help
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from report_engine import brand_from_dict, load_default_brand, model_from_dict, render_report
from report_engine.model import ReportModelError

DEFAULT_OUT_DIR = "./blazemeter-reports"


def _slug(text: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").strip().lower()).strip("-")
    return s or "report"


def _output_name(model) -> str:
    # Filesystem-safe, sortable: <title-slug>-<generated_at-slug>.html
    return "%s-%s.html" % (_slug(model.meta.title), _slug(model.meta.generated_at))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a BlazeMeter Report data model (JSON) into a self-contained, "
        "branded HTML Report.",
    )
    parser.add_argument("--model", metavar="JSON", help="Path to the Report data model JSON file.")
    parser.add_argument(
        "--out", metavar="DIR", default=DEFAULT_OUT_DIR,
        help="Output directory for the HTML Report (default: %(default)s).",
    )
    parser.add_argument(
        "--brand", metavar="JSON",
        help="Optional Brand Config JSON to override the default approximated branding "
        "(a sibling logo.svg is used if present, else an inline logo_svg in the JSON).",
    )
    parser.add_argument("--logo", metavar="SVG", help="Optional inline-SVG logo file for the brand.")
    parser.add_argument("--filename", metavar="NAME", help="Override the output filename.")
    parser.add_argument(
        "--stdout", action="store_true",
        help="Write the HTML to stdout instead of a file (ignores --out/--filename).",
    )
    args = parser.parse_args(argv)

    if not args.model:
        parser.error("--model is required to render a report")

    try:
        raw = json.loads(Path(args.model).read_text(encoding="utf-8"))
    except FileNotFoundError:
        print("error: model file not found: %s" % args.model, file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("error: --model is not valid JSON (%s)" % exc.msg, file=sys.stderr)
        return 1

    try:
        model = model_from_dict(raw)
    except ReportModelError as exc:
        print("error: invalid report model: %s" % exc, file=sys.stderr)
        return 1

    brand = load_default_brand()
    if args.brand:
        try:
            bdata = json.loads(Path(args.brand).read_text(encoding="utf-8"))
            logo_svg = Path(args.logo).read_text(encoding="utf-8") if args.logo else None
            if logo_svg is None and "logo_svg" not in bdata:
                sibling = Path(args.brand).parent / "logo.svg"
                if sibling.is_file():
                    logo_svg = sibling.read_text(encoding="utf-8")
            brand = brand_from_dict(bdata, logo_svg=logo_svg)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print("error: invalid --brand config: %s" % exc, file=sys.stderr)
            return 1

    html = render_report(model, brand)

    if args.stdout:
        sys.stdout.write(html)
        return 0

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / (args.filename or _output_name(model))
    out_path.write_text(html, encoding="utf-8")
    print("Wrote report: %s" % out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
