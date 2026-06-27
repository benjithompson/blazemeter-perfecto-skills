/*
 * Golden-test harness for the in-skill Report template engine
 * (skills/blazemeter-report/assets/report-template.html, ADR-0014).
 *
 * The Report renders CLIENT-SIDE: the template ships a vendored, dependency-free
 * JS engine that builds every section from `window.REPORT_DATA` at open time.
 * There is no Python renderer to unit-test, so this harness exercises the engine
 * at its real seam — the data model in, the rendered DOM out — by running the
 * template's own engine script under Node against a tiny, dependency-free DOM
 * shim (no jsdom, no network). It is the test-side analogue of "open the file in
 * a browser", made deterministic and assertable.
 *
 * Usage:  node render_report.mjs <template.html> <model.json>
 * Output (stdout): JSON map of { "<section id>": "<indented DOM text dump>" }.
 * The dump is what the Python golden tests assert against.
 */
import fs from "fs";

// --- minimal DOM shim ------------------------------------------------------
// Just enough of the DOM for the engine: element creation, appendChild,
// className/textContent/innerHTML, setAttribute, and no-op event listeners.

class Node {
  constructor() {
    this.children = [];
    this.childNodes = [];
    this.attributes = {};
    this.style = {};
    this._text = "";
    this.parentNode = null;
  }
  appendChild(c) {
    this.children.push(c);
    this.childNodes.push(c);
    c.parentNode = this;
    return c;
  }
  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return this.attributes[k]; }
  removeAttribute(k) { delete this.attributes[k]; }
  addEventListener() {}
  set className(v) { this._class = v; this.attributes["class"] = v; }
  get className() { return this._class || ""; }
  set textContent(v) { this._text = String(v); this.children = []; this.childNodes = []; }
  get textContent() {
    if (this.children.length) return this.children.map((c) => c.textContent).join("");
    return this._text;
  }
  set innerHTML(v) { this._html = v; }
  get innerHTML() { return this._html || ""; }
  set title(v) { this._title = v; }
  get title() { return this._title || ""; }
}

function mkEl(tag) {
  const n = new Node();
  n.tagName = (tag || "").toUpperCase();
  n.nodeName = n.tagName;
  return n;
}

// The section containers the engine writes into (single + portfolio kinds).
const IDS = [
  "report-generated", "report-title", "report-subtitle", "report-context",
  "report-summary",
  // single-test sections
  "report-charts", "report-runs", "report-regressions", "report-sla", "report-endpoints",
  "report-single-sections",
  // portfolio sections
  "report-portfolio-charts", "report-scorecard", "report-incidents",
  "report-portfolio-sections",
];
const byId = {};
IDS.forEach((id) => { const n = mkEl("div"); n.id = id; byId[id] = n; });

globalThis.window = globalThis;
globalThis.Node = Node;
globalThis.document = {
  readyState: "complete",
  title: "",
  getElementById: (id) => byId[id] || null,
  createElement: (t) => mkEl(t),
  createElementNS: (_ns, t) => mkEl(t),
  createTextNode: (t) => { const n = mkEl("#text"); n._text = String(t); n.nodeType = 3; return n; },
  addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") fn(); },
  body: mkEl("body"),
};

// --- run the template's engine ---------------------------------------------

const templatePath = process.argv[2];
const modelPath = process.argv[3];
const html = fs.readFileSync(templatePath, "utf8");

// The template carries two <script> blocks: the REPORT_DATA assignment and the
// engine. We supply REPORT_DATA from the fixture and run the engine block (the
// last one) — exactly what a browser does after token substitution.
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const engine = scripts[scripts.length - 1];
window.REPORT_DATA = JSON.parse(fs.readFileSync(modelPath, "utf8"));

// eslint-disable-next-line no-eval
(0, eval)(engine);

// --- dump the rendered DOM to assertable text ------------------------------

function dump(n, depth) {
  depth = depth || 0;
  const ind = "  ".repeat(depth);
  const tag = n.tagName || n.nodeName || "#";
  const cls = n.attributes && n.attributes["class"] ? "." + n.attributes["class"] : "";
  const leaf = n._text && !n.children.length ? ' "' + n._text + '"' : "";
  let s = ind + tag + cls + leaf + "\n";
  (n.children || []).forEach((c) => { s += dump(c, depth + 1); });
  return s;
}

const out = {};
for (const id of IDS) {
  const node = byId[id];
  out[id] = {
    hidden: node.attributes.hidden != null,
    dom: dump(node),
  };
}
process.stdout.write(JSON.stringify(out, null, 1));
