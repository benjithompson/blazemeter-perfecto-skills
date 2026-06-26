"""Golden + fixture tests for the report engine (model + renderer + CLI).

The renderer is the deterministic core of the reporting feature, so it gets real
tests over external behaviour: given a Report data model, what HTML comes out —
required sections, correct KPI/regression values, populated charts, a genuinely
self-contained/offline file, a swappable brand, and no leaked credentials.
"""

import json
import os
import re
from pathlib import Path

import pytest

import render_blazemeter_report as cli
from report_engine import (
    BrandConfig,
    brand_from_dict,
    load_default_brand,
    model_from_dict,
    render_report,
)
from report_engine.model import ReportModelError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MODEL_JSON = FIXTURES / "report_model.json"
GOLDEN_HTML = FIXTURES / "report.golden.html"


@pytest.fixture(scope="module")
def model_dict():
    return json.loads(MODEL_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def html(model_dict):
    return render_report(model_from_dict(model_dict))


# --- model -------------------------------------------------------------------


def test_model_round_trips(model_dict):
    m = model_from_dict(model_dict)
    again = model_from_dict(m.to_dict())
    assert again == m


def test_missing_meta_is_rejected():
    with pytest.raises(ReportModelError):
        model_from_dict({})


def test_run_missing_required_field_is_rejected():
    bad = {"meta": {"title": "x", "generated_at": "2026-01-01"}, "runs": [{"timestamp": "t"}]}
    with pytest.raises(ReportModelError):
        model_from_dict(bad)


def test_non_numeric_kpi_is_rejected():
    bad = {
        "meta": {"title": "x", "generated_at": "2026-01-01"},
        "runs": [{"execution_id": "1", "timestamp": "t", "kpis": {"p95_ms": "fast"}}],
    }
    with pytest.raises(ReportModelError):
        model_from_dict(bad)


# --- required sections + values ----------------------------------------------


def test_required_sections_present(html):
    for needle in (
        "Executive Summary", "Trends", "Run history", "Regressions",
        "SLA / Failure-criteria compliance", "Endpoint hot spots",
        "Checkout API – Peak",  # title
        'id="report-charts"',
    ):
        assert needle in html, "missing section/marker: %r" % needle


def test_kpi_and_regression_values_present(html):
    assert "784" in html            # latest p95
    assert "1.9" in html            # latest error rate
    assert "24.2" in html           # p95 regression pct
    assert "NO-SHIP" in html        # verdict
    assert "/checkout/submit" in html  # endpoint hot spot


def test_status_pills_rendered(html):
    assert "status-fail" in html and "status-pass" in html


# --- charts are populated from data ------------------------------------------


def _extract_report_data(html_text):
    m = re.search(r"window\.REPORT_DATA = (.*?);</script>", html_text, re.DOTALL)
    assert m, "REPORT_DATA blob not found"
    return json.loads(m.group(1))


def test_charts_data_injected(html):
    data = _extract_report_data(html)
    charts = {c["id"]: c for c in data["charts"]}
    assert {"rt", "err", "rps"} <= set(charts), "expected rt/err/rps charts, got %s" % list(charts)
    # The p95 series carries the real values, including the regressed latest run.
    p95 = next(s for s in charts["rt"]["series"] if s["name"] == "p95")
    assert p95["values"][-1] == 784
    assert charts["rt"]["xLabels"][-1] == "06-26"


def test_charts_js_is_inlined(html):
    # The vendored renderer's code is present and not loaded from anywhere.
    assert "createElementNS" in html
    assert "REPORT_DATA" in html


# --- genuinely self-contained / offline --------------------------------------


def test_no_external_resource_loads(html):
    # The offline guarantee is about not FETCHING anything — no external src/href,
    # no CDN, no web fonts. (The SVG/XML namespace URI is a name, never fetched.)
    assert "<script src=" not in html
    assert "<link " not in html
    assert 'href="http' not in html
    assert 'src="http' not in html
    assert "@import" not in html
    assert "cdn." not in html
    assert "fonts.googleapis" not in html


def test_no_credentials_or_secrets_leak(html):
    lowered = html.lower()
    for pattern in (
        "blazemeter_api_key", "api_key_id", "api_key_secret",
        "authorization:", "authorization=", "basic ", "bearer ", "x-api-key",
    ):
        assert pattern not in lowered, "possible credential leak: %r" % pattern


# --- swappable brand ---------------------------------------------------------


def _custom_brand():
    data = {
        "name": "Acme Reports",
        "primary": "#abcdef", "primary_dark": "#123456", "accent": "#fedcba",
        "bg": "#ffffff", "surface": "#eeeeee", "border": "#cccccc",
        "text": "#111111", "muted": "#777777", "good": "#0a0", "warn": "#fa0", "bad": "#a00",
        "font_stack": "Georgia, serif",
    }
    return brand_from_dict(data, logo_svg="<svg viewBox='0 0 10 10'></svg>")


def test_brand_is_swappable(model_dict):
    out = render_report(model_from_dict(model_dict), _custom_brand())
    assert "--brand-primary: #abcdef;" in out
    assert "Georgia, serif" in out
    assert "Acme Reports" in out
    # The default brand colour is not present when overridden.
    assert "#1f6feb" not in out


def test_default_brand_loads():
    b = load_default_brand()
    assert isinstance(b, BrandConfig)
    assert b.primary.startswith("#")
    assert "<svg" in b.logo_svg


# --- CLI ---------------------------------------------------------------------


def test_cli_writes_self_contained_file(tmp_path, capsys):
    code = cli.main(["--model", str(MODEL_JSON), "--out", str(tmp_path)])
    assert code == 0
    files = list(tmp_path.glob("*.html"))
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert text.startswith("<!doctype html>")
    assert "Checkout API" in text


def test_cli_stdout(capsys):
    code = cli.main(["--model", str(MODEL_JSON), "--stdout"])
    out = capsys.readouterr().out
    assert code == 0
    assert out.startswith("<!doctype html>")


def test_cli_rejects_bad_model(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    code = cli.main(["--model", str(bad), "--out", str(tmp_path)])
    assert code == 1
    assert "invalid report model" in capsys.readouterr().err


# --- golden snapshot (determinism) -------------------------------------------


def test_matches_golden(html):
    # Regenerate with: BZM_UPDATE_GOLDEN=1 pytest -k golden
    if os.environ.get("BZM_UPDATE_GOLDEN"):
        GOLDEN_HTML.write_text(html, encoding="utf-8")
    assert GOLDEN_HTML.exists(), "golden missing — run with BZM_UPDATE_GOLDEN=1 once"
    assert html == GOLDEN_HTML.read_text(encoding="utf-8"), (
        "rendered HTML drifted from the golden; if intentional, regenerate with "
        "BZM_UPDATE_GOLDEN=1"
    )


def test_render_is_deterministic(model_dict):
    a = render_report(model_from_dict(model_dict))
    b = render_report(model_from_dict(model_dict))
    assert a == b
