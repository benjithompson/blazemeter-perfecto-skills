/*
 * blazemeter-report charts — a tiny, dependency-free SVG chart renderer.
 *
 * Vendored in-repo (no CDN, no third-party blob) so a Report is a single
 * self-contained file that opens offline. It draws CLIENT-SIDE from the data
 * arrays in the global `REPORT_DATA` — the renderer supplies only data; this
 * file owns the chart scaffolding.
 *
 * REPORT_DATA.charts is a list of specs:
 *   line: { id, type:"line", title, yLabel, xLabels:[...], series:[{name,color,values:[...]}] }
 *   bar:  { id, type:"bar",  title, yLabel, bars:[{label,value,color}] }
 */
(function () {
  "use strict";
  var SVGNS = "http://www.w3.org/2000/svg";
  var W = 720, H = 300, PAD = { t: 16, r: 16, b: 46, l: 56 };

  function el(tag, attrs, text) {
    var n = document.createElementNS(SVGNS, tag);
    if (attrs) { for (var k in attrs) { if (attrs[k] != null) n.setAttribute(k, attrs[k]); } }
    if (text != null) n.textContent = text;
    return n;
  }
  function fmt(v) {
    if (v == null || isNaN(v)) return "–";
    var a = Math.abs(v);
    if (a >= 1000) return (Math.round(v * 10) / 10).toLocaleString();
    if (a >= 10) return String(Math.round(v * 10) / 10);
    return String(Math.round(v * 100) / 100);
  }
  function niceMax(m) {
    if (m <= 0) return 1;
    var pow = Math.pow(10, Math.floor(Math.log10(m)));
    var n = m / pow;
    var step = n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10;
    return step * pow;
  }

  function tooltip() {
    var t = document.getElementById("bzm-tooltip");
    if (!t) {
      t = document.createElement("div");
      t.id = "bzm-tooltip";
      t.className = "chart-tooltip";
      t.style.display = "none";
      document.body.appendChild(t);
    }
    return t;
  }
  function showTip(html, evt) {
    var t = tooltip();
    t.innerHTML = html;
    t.style.display = "block";
    t.style.left = (evt.pageX + 12) + "px";
    t.style.top = (evt.pageY + 12) + "px";
  }
  function hideTip() { tooltip().style.display = "none"; }

  function frame(svg, yMax, yLabel, xLabels) {
    var plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    var ticks = 4;
    for (var i = 0; i <= ticks; i++) {
      var yv = (yMax / ticks) * i;
      var y = PAD.t + plotH - (yv / yMax) * plotH;
      svg.appendChild(el("line", { x1: PAD.l, y1: y, x2: W - PAD.r, y2: y, class: "chart-grid" }));
      svg.appendChild(el("text", { x: PAD.l - 8, y: y + 4, class: "chart-axis chart-axis-y" }, fmt(yv)));
    }
    if (yLabel) {
      var yl = el("text", { x: 14, y: PAD.t + plotH / 2, class: "chart-axis-label",
        transform: "rotate(-90 14 " + (PAD.t + plotH / 2) + ")" }, yLabel);
      svg.appendChild(yl);
    }
    var n = xLabels.length;
    for (var j = 0; j < n; j++) {
      var x = n === 1 ? PAD.l + plotW / 2 : PAD.l + (plotW * j) / (n - 1);
      svg.appendChild(el("text", { x: x, y: H - PAD.b + 20, class: "chart-axis chart-axis-x" }, xLabels[j]));
    }
    svg.appendChild(el("line", { x1: PAD.l, y1: PAD.t + plotH, x2: W - PAD.r, y2: PAD.t + plotH, class: "chart-axis-line" }));
  }

  function xAt(i, n) {
    var plotW = W - PAD.l - PAD.r;
    return n === 1 ? PAD.l + plotW / 2 : PAD.l + (plotW * i) / (n - 1);
  }
  function yAt(v, yMax) {
    var plotH = H - PAD.t - PAD.b;
    return PAD.t + plotH - (v / yMax) * plotH;
  }

  function lineChart(spec) {
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg",
      role: "img", "aria-label": spec.title || "chart" });
    var allVals = [];
    spec.series.forEach(function (s) { (s.values || []).forEach(function (v) { if (v != null) allVals.push(v); }); });
    var yMax = niceMax(allVals.length ? Math.max.apply(null, allVals) : 1);
    var n = spec.xLabels.length;
    frame(svg, yMax, spec.yLabel, spec.xLabels);
    spec.series.forEach(function (s) {
      var pts = [], coords = [];
      (s.values || []).forEach(function (v, i) {
        if (v == null) return;
        var x = xAt(i, n), y = yAt(v, yMax);
        pts.push(x + "," + y); coords.push({ x: x, y: y, v: v, i: i });
      });
      if (pts.length > 1) svg.appendChild(el("polyline", { points: pts.join(" "), class: "chart-line", stroke: s.color }));
      coords.forEach(function (c) {
        var dot = el("circle", { cx: c.x, cy: c.y, r: 4, class: "chart-dot", fill: s.color });
        dot.addEventListener("mousemove", function (e) {
          showTip("<strong>" + s.name + "</strong><br>" + spec.xLabels[c.i] + ": " + fmt(c.v), e);
        });
        dot.addEventListener("mouseleave", hideTip);
        svg.appendChild(dot);
      });
    });
    return withLegend(spec, svg, spec.series.map(function (s) { return { name: s.name, color: s.color }; }));
  }

  function barChart(spec) {
    var svg = el("svg", { viewBox: "0 0 " + W + " " + H, class: "chart-svg",
      role: "img", "aria-label": spec.title || "chart" });
    var bars = spec.bars || [];
    var yMax = niceMax(bars.length ? Math.max.apply(null, bars.map(function (b) { return Math.abs(b.value); })) : 1);
    frame(svg, yMax, spec.yLabel, bars.map(function (b) { return b.label; }));
    var plotW = W - PAD.l - PAD.r, plotH = H - PAD.t - PAD.b;
    var bw = (plotW / Math.max(bars.length, 1)) * 0.6;
    bars.forEach(function (b, i) {
      var cx = bars.length === 1 ? PAD.l + plotW / 2 : PAD.l + (plotW * i) / (bars.length - 1);
      var h = (Math.abs(b.value) / yMax) * plotH;
      var rect = el("rect", { x: cx - bw / 2, y: PAD.t + plotH - h, width: bw, height: h,
        rx: 3, class: "chart-bar", fill: b.color });
      rect.addEventListener("mousemove", function (e) { showTip("<strong>" + b.label + "</strong><br>" + fmt(b.value), e); });
      rect.addEventListener("mouseleave", hideTip);
      svg.appendChild(rect);
    });
    return withLegend(spec, svg, null);
  }

  function withLegend(spec, svg, legend) {
    var fig = document.createElement("figure");
    fig.className = "chart-card";
    if (spec.title) {
      var cap = document.createElement("figcaption");
      cap.className = "chart-title";
      cap.textContent = spec.title;
      fig.appendChild(cap);
    }
    fig.appendChild(svg);
    if (legend && legend.length > 1) {
      var lg = document.createElement("div");
      lg.className = "chart-legend";
      legend.forEach(function (s) {
        var item = document.createElement("span");
        item.className = "chart-legend-item";
        item.innerHTML = '<span class="chart-swatch" style="background:' + s.color + '"></span>' + s.name;
        lg.appendChild(item);
      });
      fig.appendChild(lg);
    }
    return fig;
  }

  function render() {
    var data = window.REPORT_DATA || {};
    var host = document.getElementById("report-charts");
    if (!host || !data.charts) return;
    data.charts.forEach(function (spec) {
      try {
        var node = spec.type === "bar" ? barChart(spec) : lineChart(spec);
        host.appendChild(node);
      } catch (err) { /* a bad spec must never blank the whole report */ }
    });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", render);
  } else {
    render();
  }
})();
