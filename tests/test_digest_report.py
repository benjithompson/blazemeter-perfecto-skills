"""Golden/fixture tests for the DIGEST report kind at the data-model seam.

The digest is the executive daily rollup rendered by the SAME vendored JS engine
in `shared/assets/report-template.html` as the single-test and
portfolio kinds — `kind: "digest"` selects its section group; the renderer is
never forked. These tests run the engine under Node against the tiny DOM shim
(`tests/assets/render_report.mjs`) and assert:

  * the digest section group renders (tiles, tree, incidents, coverage) and the
    other kinds' groups stay hidden;
  * the executive tile band carries exactly six tiles with the model's values;
  * the workspace → project → test tree nests (workspace level only when the
    model has workspace rollup rows) and its test tables have sortable headers;
  * the problems-only toggle defaults ON when any failure/regression exists and
    OFF when the window is clean;
  * a `</` inside a test name renders as data, and the documented template-fill
    escape (`</` → `<\\/`) can never close the script tag early;
  * incidents render as a FLAT ranked list and the coverage footer is always
    present;
  * the existing `report` (single) and `portfolio` kinds still render against
    the edited template with the digest sections hidden (no regression).
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = REPO / "shared" / "assets" / "report-template.html"
HARNESS = REPO / "tests" / "assets" / "render_report.mjs"
DIGEST_MODEL = REPO / "tests" / "fixtures" / "digest" / "digest-model.json"
QUIET_MODEL = REPO / "tests" / "fixtures" / "digest" / "digest-quiet-model.json"
PORTFOLIO_MODEL = REPO / "tests" / "fixtures" / "portfolio" / "portfolio-model.json"
SINGLE_MODEL = REPO / "tests" / "fixtures" / "portfolio" / "single-model.json"

NODE = shutil.which("node")
requires_node = pytest.mark.skipif(NODE is None, reason="node not available to run the JS engine")


def _render(model_path):
    proc = subprocess.run(
        [NODE, str(HARNESS), str(TEMPLATE), str(model_path)],
        capture_output=True, text=True, check=True,
    )
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def digest():
    if NODE is None:
        pytest.skip("node not available")
    return _render(DIGEST_MODEL)


@pytest.fixture(scope="module")
def quiet():
    if NODE is None:
        pytest.skip("node not available")
    return _render(QUIET_MODEL)


# --- fixtures are well-formed (runs without node) -----------------------------


def test_fixtures_are_valid_json():
    json.loads(DIGEST_MODEL.read_text())
    json.loads(QUIET_MODEL.read_text())


# --- section dispatch ---------------------------------------------------------


@requires_node
def test_digest_unhides_digest_sections_hides_others(digest):
    assert digest["report-digest-sections"]["hidden"] is False
    assert digest["digest-tiles"]["hidden"] is False
    assert digest["report-single-sections"]["hidden"] is True
    assert digest["report-portfolio-sections"]["hidden"] is True


@requires_node
def test_digest_header_context_and_verdict(digest):
    assert "Acme — Daily Digest" in digest["report-title"]["dom"]
    ctx = digest["report-context"]["dom"]
    assert "Scope" in ctx and "Whole account" in ctx and "Acme" in ctx
    summary = digest["report-summary"]["dom"]
    assert "CRITICAL" in summary
    assert "needs eyes first" in summary


# --- executive KPI tile band --------------------------------------------------


@requires_node
def test_digest_tile_band_has_exactly_six_tiles(digest):
    dom = digest["digest-tiles"]["dom"]
    assert dom.count("DIV.digest-tile\n") == 6


@requires_node
def test_digest_tile_values_copied_from_model(digest):
    dom = digest["digest-tiles"]["dom"]
    assert "81.8%" in dom                      # pass rate
    assert "Failing runs" in dom
    assert "Newly regressed tests" in dom
    assert "across 5 tests" in dom             # runs-in-window companion note
    assert '"14"' in dom                       # runs in window
    assert "top severity: critical" in dom     # incidents tile severity label
    assert "digest-value-bad" in dom           # incidents/failing colored by severity
    assert "Unjudged runs" in dom and '"3"' in dom


@requires_node
def test_quiet_tiles_show_no_incidents(quiet):
    dom = quiet["digest-tiles"]["dom"]
    assert "none flagged" in dom
    assert "100%" in dom


# --- workspace → project → test tree ------------------------------------------


@requires_node
def test_tree_nests_workspace_project_test(digest):
    dom = digest["digest-tree"]["dom"]
    # 2 workspace nodes + 3 project nodes, all <details> (expand-in-place).
    assert dom.count("DETAILS.digest-node") == 5
    assert dom.count('"Workspace"') == 2 and dom.count('"Project"') == 3
    # Nesting order: workspace before its project before its tests.
    assert dom.index("Retail") < dom.index("Checkout") < dom.index("checkout-flow")
    assert dom.index("Internal Tools") < dom.index("Admin") < dom.index("admin-portal")
    # Rollup stats and health chips on the nodes.
    assert "9 runs · 3 tests · 6 passed · 2 failed" in dom
    assert "health-critical" in dom and "health-unknown" in dom  # unjudged chip


@requires_node
def test_tree_skips_workspace_level_at_project_scope(quiet):
    dom = quiet["digest-tree"]["dom"]
    assert '"Workspace"' not in dom
    assert dom.count("DETAILS.digest-node") == 1  # just the project node
    assert "storefront-browse" in dom and "storefront-buy" in dom


@requires_node
def test_tree_test_tables_have_sortable_headers(digest):
    dom = digest["digest-tree"]["dom"]
    assert "TH.digest-sort-th" in dom
    for col in ["Test", "Health", "Runs", "Passed", "Failed", "Worst KPI move", "Baseline", "Note"]:
        assert f'"{col}"' in dom
    assert "p95 +34%" in dom and "error rate +26.3 pts" in dom
    assert "committed file" in dom and "no baseline" in dom


@requires_node
def test_problems_only_defaults_on_when_failures_exist(digest):
    assert "DIV.digest-problems-on" in digest["digest-tree"]["dom"]
    toolbar = digest["digest-toolbar"]["dom"]
    assert "Problems only" in toolbar
    assert "3 healthy/unjudged tests hidden" in toolbar


@requires_node
def test_problems_only_defaults_off_when_clean(quiet):
    assert "DIV.digest-problems-off" in quiet["digest-tree"]["dom"]
    assert "showing all tests" in quiet["digest-toolbar"]["dom"]


# --- incidents (flat) and coverage footer -------------------------------------


@requires_node
def test_incidents_are_flat_ranked_never_nested(digest):
    dom = digest["digest-incidents"]["dom"]
    assert "UL.reg-list" in dom
    assert "DETAILS" not in dom  # never nested under the tree
    assert "sev-critical" in dom and "sev-warning" in dom and "sev-info" in dom
    assert "checkout-flow" in dom and "run ex-9911" in dom


@requires_node
def test_incidents_empty_state(quiet):
    assert "No incidents flagged." in quiet["digest-incidents"]["dom"]


@requires_node
def test_coverage_footer_always_rendered(digest, quiet):
    dom = digest["digest-coverage"]["dom"]
    assert "Fetch coverage:" in dom and "38 of 40" in dom and "the digest is partial" in dom
    assert "KPIs never folded in" in dom
    assert "not judged, not green" in dom
    assert "insufficient data, not a finding" in dom
    # Rendered even when everything is zero.
    quiet_dom = quiet["digest-coverage"]["dom"]
    assert "Fetch coverage:" in quiet_dom and "12 of 12" in quiet_dom


# --- script-tag safety ---------------------------------------------------------


@requires_node
def test_slash_in_test_name_renders_as_data(digest):
    assert "cart-service </checkout>" in digest["digest-tree"]["dom"]
    assert "cart-service </checkout>" in digest["digest-incidents"]["dom"]


def test_template_fill_escape_keeps_script_tag_closed():
    # The documented fill: serialize the model, escape every "</" as "<\\/",
    # replace the single token. The payload must not be able to close the
    # template's <script> tag early even with "</checkout>" in a test name.
    template = TEMPLATE.read_text()
    model = json.loads(DIGEST_MODEL.read_text())
    payload = json.dumps(model).replace("</", "<\\/")
    assert "</" not in payload
    filled = template.replace("{{REPORT_DATA_JSON}}", payload)
    assert filled.count("</script>") == template.count("</script>")
    assert filled.count("{{REPORT_DATA_JSON}}") == 0


# --- no regression: single + portfolio kinds against the edited template ------


@requires_node
def test_single_kind_still_renders_and_digest_stays_hidden():
    out = _render(SINGLE_MODEL)
    assert out["report-single-sections"]["hidden"] is False
    assert out["report-digest-sections"]["hidden"] is True
    assert out["digest-tiles"]["hidden"] is True
    assert "Response time (ms)" in out["report-charts"]["dom"]


@requires_node
def test_portfolio_kind_still_renders_and_digest_stays_hidden():
    out = _render(PORTFOLIO_MODEL)
    assert out["report-portfolio-sections"]["hidden"] is False
    assert out["report-digest-sections"]["hidden"] is True
    assert out["digest-tiles"]["hidden"] is True
    assert "SLA compliance by test (%)" in out["report-portfolio-charts"]["dom"]
    assert out["digest-tree"]["dom"].strip().startswith("DIV")  # left empty


# --- no leaked credentials ------------------------------------------------------

SECRET_MARKERS = [
    "API_KEY_ID", "API_KEY_SECRET", "BLAZEMETER_API_KEY",
    "api_key", "apikey", "secret", "password", "passwd",
    "authorization", "bearer", "x-api-key", "private_key", "access_token",
]


def test_no_secrets_in_digest_fixtures():
    for path in (DIGEST_MODEL, QUIET_MODEL):
        low = path.read_text().lower()
        for marker in SECRET_MARKERS:
            assert marker.lower() not in low, f"possible credential marker {marker!r} in {path.name}"


@requires_node
def test_no_secrets_in_rendered_digest(digest):
    low = json.dumps(digest).lower()
    for marker in SECRET_MARKERS:
        assert marker.lower() not in low, f"possible credential marker {marker!r} in rendered digest"
