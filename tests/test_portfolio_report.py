"""Golden/fixture tests for the Report template engine at the data-model seam.

The Report is rendered CLIENT-SIDE by a vendored JS engine baked into
`shared/assets/report-template.html` (ADR-0014). New report
types are added at the **same data-model seam** rather than by forking the
renderer: the model's `kind` field selects which section group the one engine
builds. These tests exercise that seam — fixture data model in, rendered DOM out
— by running the template's own engine under Node against a tiny DOM shim
(`tests/assets/render_report.mjs`), the assertable analogue of opening the file.

Coverage:
  * the PORTFOLIO kind renders its scorecard / incidents / charts with the right
    per-test KPI, SLA-compliance %, health, trend, and regression-flag values,
    and shows the cross-test scope (account/workspace/project + test count);
  * the SINGLE-test kind STILL renders through the same engine (proof the
    portfolio type was added additively, without breaking the v1 path);
  * NO credential/secret ever appears in the rendered HTML (the model holds data
    + narrative only; conventions §6).

If Node is unavailable the rendering tests skip (the engine is JS); the
no-leaked-credentials text scan over the template + fixtures still runs.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "shared" / "assets" / "report-template.html"
HARNESS = REPO / "tests" / "assets" / "render_report.mjs"
FIXTURES = REPO / "tests" / "fixtures" / "portfolio"
PORTFOLIO_MODEL = FIXTURES / "portfolio-model.json"
SINGLE_MODEL = FIXTURES / "single-model.json"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not available to run the JS engine")


def _render(model_path):
    """Run the template engine under Node and return {section_id: {hidden, dom}}."""
    proc = subprocess.run(
        [NODE, str(HARNESS), str(TEMPLATE), str(model_path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def portfolio():
    if NODE is None:
        pytest.skip("node not available")
    return _render(PORTFOLIO_MODEL)


@pytest.fixture(scope="module")
def single():
    if NODE is None:
        pytest.skip("node not available")
    return _render(SINGLE_MODEL)


# --- fixtures / assets are well-formed (runs without node) -------------------


def test_template_and_harness_exist():
    assert TEMPLATE.is_file()
    assert HARNESS.is_file()
    assert PORTFOLIO_MODEL.is_file() and SINGLE_MODEL.is_file()


def test_fixtures_are_valid_json():
    json.loads(PORTFOLIO_MODEL.read_text())
    json.loads(SINGLE_MODEL.read_text())


def test_template_has_single_data_token():
    # The skill substitutes exactly one token; more than one would break the fill.
    assert TEMPLATE.read_text().count("{{REPORT_DATA_JSON}}") == 1


# --- PORTFOLIO kind: required sections present -------------------------------


@requires_node
def test_portfolio_unhides_portfolio_sections_hides_single(portfolio):
    assert portfolio["report-portfolio-sections"]["hidden"] is False
    assert portfolio["report-single-sections"]["hidden"] is True


@requires_node
def test_portfolio_scorecard_has_a_row_per_test(portfolio):
    dom = portfolio["report-scorecard"]["dom"]
    # 5 tests in the fixture → 5 table rows in the tbody.
    assert dom.count("TR") == 5
    for name in ["Checkout Flow", "Product Search", "Login", "Cart Update"]:
        assert name in dom


@requires_node
def test_portfolio_scorecard_kpi_and_sla_values(portfolio):
    dom = portfolio["report-scorecard"]["dom"]
    # SLA-compliance % rendered per test (1 decimal, integer shown bare).
    assert "57.1%" in dom   # Checkout
    assert "100%" in dom    # Search (whole number → no decimals)
    assert "88.9%" in dom   # Login
    assert "94.4%" in dom   # Cart
    # run counts and worst-move strings surface.
    assert '"14"' in dom and '"22"' in dom
    assert "p95 +34%" in dom


@requires_node
def test_portfolio_health_pills_and_trend_arrows(portfolio):
    dom = portfolio["report-scorecard"]["dom"]
    assert "health-critical" in dom and "health-healthy" in dom and "health-at-risk" in dom
    assert "trend-degrading" in dom and "trend-improving" in dom and "trend-stable" in dom
    assert "▼ degrading" in dom and "▲ improving" in dom


@requires_node
def test_portfolio_regression_flags(portfolio):
    dom = portfolio["report-scorecard"]["dom"]
    # Checkout is flagged regressed; the healthy tests are not.
    assert "flag-yes" in dom
    assert "flag-no" in dom


@requires_node
def test_portfolio_incidents_ranked_with_severity(portfolio):
    dom = portfolio["report-incidents"]["dom"]
    assert "sev-critical" in dom and "sev-warning" in dom
    assert "Checkout Flow" in dom
    assert "run ex-9911" in dom


@requires_node
def test_portfolio_charts_are_populated(portfolio):
    dom = portfolio["report-portfolio-charts"]["dom"]
    # Two derived bar charts: SLA compliance by test + health distribution.
    assert "SLA compliance by test (%)" in dom
    assert "Test health distribution" in dom
    assert dom.count("SVG.chart-svg") == 2
    assert "CHART-BAR" in dom.upper() or "chart-bar" in dom


@requires_node
def test_portfolio_context_shows_scope_and_test_count(portfolio):
    dom = portfolio["report-context"]["dom"]
    assert "Acme" in dom and "Web Performance" in dom and "Storefront" in dom
    assert "5 in scope" in dom


@requires_node
def test_portfolio_header_and_summary(portfolio):
    assert "Acme Web — Performance Portfolio" in portfolio["report-title"]["dom"]
    summary = portfolio["report-summary"]["dom"]
    assert "REGRESSED" in summary


# --- SINGLE kind still renders through the same engine (no regression) -------


@requires_node
def test_single_kind_still_renders_runs_and_hides_portfolio(single):
    assert single["report-single-sections"]["hidden"] is False
    assert single["report-portfolio-sections"]["hidden"] is True
    runs = single["report-runs"]["dom"]
    assert "May 28" in runs and "Jun 27" in runs
    assert "642" in runs  # latest p95
    # single-test charts (trend lines) still build.
    assert "Response time (ms)" in single["report-charts"]["dom"]
    # SLA + endpoints sections still populate on the single path.
    assert "p95 under 600ms" in single["report-sla"]["dom"]
    assert "/checkout" in single["report-endpoints"]["dom"]


@requires_node
def test_single_kind_leaves_portfolio_sections_empty(single):
    assert "TABLE" not in single["report-scorecard"]["dom"]
    assert "SVG" not in single["report-portfolio-charts"]["dom"]


# --- no leaked credentials / secrets ----------------------------------------

# Tokens that must never appear in a rendered report or the template/fixtures.
SECRET_MARKERS = [
    "API_KEY_ID", "API_KEY_SECRET", "BLAZEMETER_API_KEY",
    "api_key", "apikey", "secret", "password", "passwd",
    "authorization", "bearer", "x-api-key", "private_key", "access_token",
]


def _assert_no_secrets(text, where):
    low = text.lower()
    for marker in SECRET_MARKERS:
        assert marker.lower() not in low, f"possible credential marker {marker!r} leaked in {where}"


def test_no_secrets_in_template_or_fixtures():
    _assert_no_secrets(TEMPLATE.read_text(), "report-template.html")
    _assert_no_secrets(PORTFOLIO_MODEL.read_text(), "portfolio-model.json")
    _assert_no_secrets(SINGLE_MODEL.read_text(), "single-model.json")


@requires_node
def test_no_secrets_in_rendered_portfolio(portfolio):
    rendered = json.dumps(portfolio)
    _assert_no_secrets(rendered, "rendered portfolio DOM")


@requires_node
def test_script_injection_in_label_cannot_break_out(portfolio):
    # A test label containing "</checkout>" is data, not markup: it renders as
    # text in the scorecard, never as a closed tag.
    dom = portfolio["report-scorecard"]["dom"]
    assert "Catalog Browse </checkout>" in dom
