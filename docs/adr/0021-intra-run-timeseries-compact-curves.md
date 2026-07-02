# Intra-run timeseries: one kpi-values pull per run, downsampled curves + shape summary

The trend and compare modes of `bzm-test-analysis` compared runs only by their whole-run
aggregate KPIs; how a run *unfolded* — the Intra-Run Timeseries behind the platform's live
execution charts — was invisible, so questions like "does p95 degrade at minute 3 of every
run?" couldn't be answered. The endpoints exist but are outside the MCP's surface, and raw
series data is O(runs × datapoints) — folding it in naively would violate the engine
invariant that the model only ever ingests one compact pre-aggregated JSON.

Decision, as an opt-in `--timeseries` on the engine's `history` and `run-pair` subcommands:

- **One series request per run, at the coarsest documented interval.** Per selected run the
  engine makes two GETs: `/data/labels` to find the aggregate ALL label, then a single
  `/masters/{id}/kpi-values` pull at `interval=60` — its datapoints carry the full multi-KPI
  field set (users, hits, errors, avg, percentiles), so one request covers every curve, and
  it is the only endpoint family with per-bucket percentiles. The richer-looking
  alternatives are deliberately unused: the `timeline/kpis` catalog tree is a more complex
  contract than the flat label list, and `data/kpis`'s window semantics on completed masters
  are unverified.
- **Bounded output, two layers.** Each selected run gets a `timeseries` block holding (a) a
  curve downsampled to ≤ 60 column-array points (counts summed, gauges peaked, avg
  hits-weighted, merged p95 = worst bucket so spikes survive merging) and (b) a
  deterministic `shape` summary (ramp, steady-phase splits, least-squares p95 slope, RT
  spikes, error bursts, saturation knee) computed from the full minute series before
  merging. The AI reasons over shapes across runs; it never sees raw datapoints.
- **Bounded selection.** `history` pulls curves only for the newest `--curve-runs` (default
  5) KPI runs, always force-including the candidate and an in-window baseline. Cost and
  JSON size are O(curve-runs), independent of window depth. `sweep` (cross-test) gets no
  timeseries at all.
- **Degrade, never fail.** A missing ALL label, empty series, or fetch failure yields
  `timeseries: null` + a `timeseries_unavailable` note and lands in `coverage`; absence of
  the key means the run simply wasn't selected.

## Consequences

- Output schema bumps to v4 (additive: optional `timeseries` blocks).
- The endpoints are doc-verified but not yet live-verified (no credentials at
  implementation time): `test_live_intra_run_timeseries_endpoints` pins the assumptions —
  flat ALL-bearing `/data/labels`, ts-bearing multi-KPI datapoints — and auto-runs when
  credentials are present, per the live-tripwire posture of ADR-0013. If live behavior
  contradicts them, fixtures and API_NOTES get corrected rather than the skill guessing.
- The branded HTML template still charts only the cross-run trend; within-run curve
  sections are a possible later enhancement, and until then intra-run findings reach
  reports as narrative prose.
