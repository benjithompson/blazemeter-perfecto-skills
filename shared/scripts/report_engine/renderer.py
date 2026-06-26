"""The pure renderer: ``ReportModel`` → single-file, self-contained branded HTML.

Determinism lives here and in the template (ADR-0005): given the same model +
brand, the output is byte-identical (``generated_at`` is part of the model, not
read from the clock). The renderer **only ever reads the model and the brand** —
it never touches the network, the environment, or credentials, so a Report can
never leak a secret it was never given.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Iterable

from .brand import BrandConfig, load_default_brand
from .model import (
    EndpointRow,
    Regression,
    ReportModel,
    RunRow,
    SlaCompliance,
)

ASSETS = Path(__file__).resolve().parent / "assets"
TEMPLATE = ASSETS / "template.html"
CHARTS_JS = ASSETS / "charts.js"


# --- small formatting helpers ------------------------------------------------


def _esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def _num(v: float | None, *, suffix: str = "", decimals: int = 1) -> str:
    if v is None:
        return '<span class="muted">–</span>'
    rounded = round(float(v), decimals)
    if rounded == int(rounded):
        body = "{:,}".format(int(rounded))
    else:
        body = "{:,.{d}f}".format(rounded, d=decimals)
    return _esc(body + suffix)


def _short_ts(run: RunRow) -> str:
    if run.label:
        return run.label
    ts = run.timestamp
    return (ts[:16].replace("T", " ")) if ts else run.execution_id


def _status_pill(status: str) -> str:
    s = (status or "").lower()
    cls = "status-pass" if s == "pass" else "status-fail" if s in ("fail", "error", "abort") else "status-other"
    return '<span class="status-pill %s">%s</span>' % (cls, _esc(status or "—"))


# --- section builders --------------------------------------------------------


def _context_rows(model: ReportModel) -> str:
    ctx = model.meta.context
    parts = []
    for label, ref in (("Account", ctx.account), ("Workspace", ctx.workspace),
                       ("Project", ctx.project), ("Test", ctx.test)):
        if ref is None:
            continue
        idtxt = " <span class=\"muted\">(%s)</span>" % _esc(ref.id) if ref.id else ""
        parts.append("<span><b>%s</b> %s%s</span>" % (_esc(label), _esc(ref.name), idtxt))
    return "".join(parts) or '<span class="empty">No context supplied.</span>'


def _verdict_class(verdict: str) -> str:
    v = (verdict or "").upper()
    if any(k in v for k in ("SHIP", "PASS", "STABLE", "GREEN", "OK")) and "NO-SHIP" not in v and "NO SHIP" not in v:
        return "good"
    if any(k in v for k in ("NO-SHIP", "NO SHIP", "FAIL", "REGRESS", "RED", "BLOCK")):
        return "bad"
    if any(k in v for k in ("WARN", "WATCH", "CAUTION", "AMBER")):
        return "warn"
    return ""


def _summary(model: ReportModel) -> str:
    s = model.summary
    bits = []
    if s.verdict:
        bits.append('<p><span class="verdict-badge %s">%s</span></p>' % (_verdict_class(s.verdict), _esc(s.verdict)))
    if s.headline:
        bits.append("<p><strong>%s</strong></p>" % _esc(s.headline))
    if s.narrative:
        bits.append('<div class="narrative">%s</div>' % "".join("<p>%s</p>" % _esc(p) for p in s.narrative))
    return "".join(bits) or '<span class="empty">No summary provided.</span>'


def _trend_table(runs: Iterable[RunRow]) -> str:
    runs = list(runs)
    if not runs:
        return '<span class="empty">No runs in this report.</span>'
    head = ("<thead><tr><th>Run</th><th>Avg RT</th><th>p90</th><th>p95</th><th>p99</th>"
            "<th>RPS</th><th>Err %</th><th>Status</th></tr></thead>")
    rows = []
    for r in runs:
        k = r.kpis
        rows.append(
            "<tr><td title=\"%s\">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
            % (
                _esc("execution %s" % r.execution_id), _esc(_short_ts(r)),
                _num(k.avg_rt_ms), _num(k.p90_ms), _num(k.p95_ms), _num(k.p99_ms),
                _num(k.rps, decimals=2), _num(k.error_rate_pct, decimals=2), _status_pill(r.status),
            )
        )
    return "<table>%s<tbody>%s</tbody></table>" % (head, "".join(rows))


def _regressions(regs: Iterable[Regression]) -> str:
    regs = list(regs)
    if not regs:
        return '<span class="empty">No regressions flagged.</span>'
    items = []
    for r in regs:
        sev = r.severity if r.severity in ("info", "warning", "critical") else "info"
        arrow = "▲" if r.direction == "up" else "▼" if r.direction == "down" else "→"
        dcls = "delta-up" if r.direction == "up" else "delta-down" if r.direction == "down" else ""
        note = ' <span class="muted">— %s</span>' % _esc(r.note) if r.note else ""
        items.append(
            '<li class="reg-item"><span class="sev-dot sev-%s"></span>'
            '<span class="reg-kpi">%s</span>'
            '<span class="reg-delta">%s → %s '
            '<span class="%s">(%s %s%%)</span></span>%s</li>'
            % (sev, _esc(r.kpi), _num(r.from_value, decimals=2), _num(r.to_value, decimals=2),
               dcls, arrow, _num(abs(r.pct_change), decimals=1), note)
        )
    return '<ul class="reg-list">%s</ul>' % "".join(items)


def _sla(sla: SlaCompliance | None) -> str:
    if sla is None:
        return '<span class="empty">No SLA / failure criteria evaluated.</span>'
    total = sla.pass_count + sla.fail_count
    head = "<p><strong>%d</strong> passed, <strong>%d</strong> failed%s</p>" % (
        sla.pass_count, sla.fail_count,
        (" of %d runs" % total) if total else "",
    )
    if not sla.rules:
        return head
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (_esc(r.label), _esc(r.threshold), _num(r.pass_rate_pct, suffix="%"),
           _esc(r.note) if r.note else '<span class="muted">—</span>')
        for r in sla.rules
    )
    table = ("<table><thead><tr><th>Criterion</th><th>Threshold</th><th>Pass rate</th><th>Note</th></tr></thead>"
             "<tbody>%s</tbody></table>" % rows)
    return head + table


def _endpoints(eps: Iterable[EndpointRow]) -> str:
    eps = list(eps)
    if not eps:
        return '<span class="empty">No per-endpoint breakdown.</span>'
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>"
        % (_esc(e.name), _num(e.p95_ms), _num(e.error_rate_pct, decimals=2),
           _esc(e.trend) if e.trend else '<span class="muted">—</span>')
        for e in eps
    )
    # Keep the header (which contains a literal '%') out of the %-format string.
    head = "<thead><tr><th>Endpoint</th><th>p95</th><th>Err %</th><th>Trend</th></tr></thead>"
    return "<table>%s<tbody>%s</tbody></table>" % (head, rows)


# --- chart specs derived from the data (the renderer owns this, not the model) ---


def _build_charts(model: ReportModel, brand: BrandConfig) -> list[dict]:
    runs = model.runs
    if not runs:
        return []
    xlabels = [_short_ts(r) for r in runs]

    def col(attr):
        return [getattr(r.kpis, attr) for r in runs]

    charts: list[dict] = []

    rt_series = [
        ("p90", col("p90_ms"), brand.primary),
        ("p95", col("p95_ms"), brand.accent),
        ("p99", col("p99_ms"), brand.bad),
    ]
    rt_series = [(n, vals, c) for (n, vals, c) in rt_series if any(v is not None for v in vals)]
    if rt_series:
        charts.append({
            "id": "rt", "type": "line", "title": "Response time (ms)", "yLabel": "ms",
            "xLabels": xlabels,
            "series": [{"name": n, "color": c, "values": vals} for (n, vals, c) in rt_series],
        })

    if any(r.kpis.error_rate_pct is not None for r in runs):
        charts.append({
            "id": "err", "type": "line", "title": "Error rate (%)", "yLabel": "%",
            "xLabels": xlabels,
            "series": [{"name": "error %", "color": brand.bad, "values": col("error_rate_pct")}],
        })

    if any(r.kpis.rps is not None for r in runs):
        charts.append({
            "id": "rps", "type": "line", "title": "Throughput (RPS)", "yLabel": "req/s",
            "xLabels": xlabels,
            "series": [{"name": "RPS", "color": brand.primary, "values": col("rps")}],
        })

    return charts


# --- the public renderer -----------------------------------------------------


def render_report(model: ReportModel, brand: BrandConfig | None = None) -> str:
    """Render a ``ReportModel`` to a single self-contained HTML string."""
    brand = brand or load_default_brand()

    report_data = {"charts": _build_charts(model, brand)}
    # Embed JSON safely inside a <script>: neutralize any "</" so a string value
    # can never close the tag early.
    data_json = json.dumps(report_data, ensure_ascii=False).replace("</", "<\\/")

    window = ""
    if model.meta.window_start or model.meta.window_end:
        window = " · window %s → %s" % (
            _esc(model.meta.window_start or "…"), _esc(model.meta.window_end or "…"))

    replacements = {
        "{{BRAND_CSS_VARS}}": brand.css_variables(),
        "{{BRAND_NAME}}": _esc(brand.name),
        "{{LOGO_SVG}}": brand.logo_svg,
        "{{TITLE}}": _esc(model.meta.title),
        "{{SUBTITLE}}": _esc(model.meta.subtitle),
        "{{GENERATED_AT}}": _esc(model.meta.generated_at),
        "{{WINDOW}}": window,
        "{{CONTEXT_ROWS}}": _context_rows(model),
        "{{SUMMARY}}": _summary(model),
        "{{TREND_TABLE}}": _trend_table(model.runs),
        "{{REGRESSIONS}}": _regressions(model.regressions),
        "{{SLA}}": _sla(model.sla),
        "{{ENDPOINTS}}": _endpoints(model.endpoints),
        "{{REPORT_DATA_JSON}}": data_json,
        "{{CHARTS_JS}}": CHARTS_JS.read_text(encoding="utf-8"),
    }

    html_out = TEMPLATE.read_text(encoding="utf-8")
    for token, value in replacements.items():
        html_out = html_out.replace(token, value)
    return html_out
