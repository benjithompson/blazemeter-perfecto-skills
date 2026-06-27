# Report engine: self-contained HTML, vendored charts, swappable brand config

> **Status:** Superseded by [ADR-0014](0014-report-as-in-skill-model-filled-html-template.md) — there
> is no Python report engine and no separate swappable brand-config file. The self-contained-HTML and
> vendored-charts guarantees are retained, but the template is filled in-skill and branding is baked
> into the template (re-brand = edit the template's CSS vars + inline logo).

Reports are generated as **single-file, self-contained HTML** — a vendored lightweight charting
library and all chart scaffolding live in the branded template; the AI supplies only the data arrays
and narrative. The file opens anywhere, offline, and is safe to email.

Branding lives in a **swappable brand config** (colors, logo, fonts) that the template reads. v1
ships **approximated** BlazeMeter branding; switching to official assets later is a config/logo swap,
not a template rewrite.

Rejected: CDN-linked chart libs (break offline, not a clean single-file artifact) and pre-rendered
static images (need a Python/matplotlib toolchain, non-interactive).

Reports write to a configurable output directory (default `./blazemeter-reports/`).
