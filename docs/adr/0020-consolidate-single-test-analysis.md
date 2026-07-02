# Consolidate single-test analysis into one skill, split baseline by read/write

Single-test cross-run analysis was spread over four skills that differed by mode or output
shape rather than by job: `bzm-analyze-test` (prose trend), `bzm-report` (HTML trend),
`bzm-compare-runs` (two-run diff), and `bzm-baseline` (pin/resolve/show/write). Users had to
know our skill taxonomy to ask a question about one test. We consolidated them into a single
skill, **`bzm-test-analysis`**, with mode (trend over a window, compare-vs-baseline,
pair-compare) and output format (prose, or branded HTML) as options — mode inferred from the
prompt when clear, offered as an option list only when ambiguous. Default window stays last
30 days with an explicit "all history" escape hatch.

The read/write seam is the one boundary we kept: **`bzm-set-baseline`** (renamed from
`bzm-baseline`) is purely the writer of the committed `.blazemeter/baseline.json`; all
read-path baseline behavior — resolution (pin → committed file → last passing) and the
conversational pin — lives in `bzm-test-analysis`. An "analyze" skill silently writing a
committed file into the user's repo would be surprising; the split keeps analysis read-only.

## Consequences

- `bzm-compare-runs` and `bzm-report` are deleted; `bzm-pr-gate`'s delegation targets and
  `bzm-ci-setup`'s pointers (including strings emitted by `bzm_ci_scaffold.py`) are rewired
  to the two surviving skills. The shared engines (`bzm_fetch.py` `history`/`run-pair`,
  `bzm_baseline.py`) are unchanged.
- The Branded Report Template moves from `skills/bzm-report/assets/` to `shared/assets/`,
  since `bzm-daily-digest` and `bzm-portfolio-report` also fill it; a compare kind is added
  so the pair/baseline modes can emit HTML.
- `bzm-triage-failure` deliberately stays separate: it is within-run diagnosis of a single
  execution, a different job from cross-run comparison, and merging it would blur the
  consolidated skill's trigger description.
- Intra-run timeseries (the data behind the platform's live execution charts) is deferred to
  a phase-2 enhancement: the endpoint is undocumented in this repo and unavailable via the
  MCP, so it needs its own research pass (API_NOTES row, fetch subcommand, fixtures,
  downsampling design) before the skill can use it.
