# Report renders from an in-skill, model-filled, self-contained HTML template (no local interpreter)

The `blazemeter-report` skill produces its HTML by filling a **static, self-contained template
shipped inside the skill** (`skills/blazemeter-report/assets/report-template.html`) with the Report
data model, rather than shelling out to a Python renderer. The skill reads the template, replaces a
single `{{REPORT_DATA_JSON}}` token with the model JSON (escaping `</` to `<\/` so a string value
can't close the `<script>` tag), and writes the `.html`. The template's baked-in CSS, brand vars,
inline logo, and vendored client-side JS build **every** section — context, summary, run history,
regressions, SLA, endpoints, and the trend charts derived from `runs[]` — from
`window.REPORT_DATA` at open time.

**Why:** zero local-interpreter dependency. The skill now runs identically across the Claude Code
CLI, VS Code, and the desktop app — no `python` needs to resolve on the local-session `PATH`. The
artifact stays a single offline, self-contained file, and determinism is preserved (only the data
varies; layout and branding are fixed in the template).

**Trade-off:** the section-building logic moves from tested Python (`report_engine`) into in-template
JS that is not unit-tested; correctness is verified by opening a generated report. Re-branding is now
a template edit (CSS `:root` vars + inline logo SVG) instead of a swappable brand-config file.

Supersedes:
- **ADR-0005** (reporting via a deterministic branded *template* rendered by the AI-supplied data) —
  superseded in mechanism: the template is filled in-skill, not by a Python renderer. The
  reporting-as-first-class-value-area intent stands.
- **ADR-0007** (hybrid shared logic) — superseded **only** for the report template/renderer: the
  branded template no longer lives in `shared/scripts/` as centralized code; it ships inside the
  skill's `assets/`. The centralized `bzm-*` auth/artifact utilities are unaffected.
- **ADR-0009** (report engine: self-contained HTML, vendored charts, swappable brand config) —
  superseded: the vendored charts and self-contained/offline guarantees are kept, but there is no
  Python report engine and no separate swappable brand-config file; branding is baked into the
  template.
