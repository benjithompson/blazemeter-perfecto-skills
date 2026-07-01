---
name: bzm-report
description: Generate a branded, self-contained HTML cross-run trend & regression Report for a BlazeMeter test over a time window — trend lines, regression flags, and SLA compliance across many runs. Use when asked for a shareable/stakeholder report, a release report, a multi-run trend or regression summary, or a portfolio/scorecard view the platform's single-run reports can't produce.
---

Produce the flagship **Report**: retrieve many runs of one test over a window, shape them into the **Report data model**, and emit a branded, self-contained HTML file that surfaces trends, regressions, and SLA compliance across the window — the cross-execution/time view BlazeMeter's single-run reports can't give you. (For a report across **many tests**, use `bzm-portfolio-report` — same template, one row per test instead of one row per run.)

**Division of labor (important):** the MCP is used for the *control plane* — resolving the test interactively, the AI-consent gate, the test object's SLA rules, and single-run drill-ins (including the latest run's endpoint breakdown). The *bulk data pull* (listing every execution in the window, fetching every run's reports) is **never** done by chaining MCP calls — a busy test's window is dozens of runs and hundreds of payloads. It is handed off to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py`, whose `history` subcommand pulls the test's runs from the BlazeMeter API directly, does all the arithmetic (window filtering, status bucketing, per-run KPIs, baseline resolution, per-run deltas, normalization), and returns one compact pre-aggregated JSON. You read only that JSON, map it into the Report data model, and fill the shipped HTML template — the render is a token replacement plus a file write, no local interpreter.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

Always resolve and **display** the full context (with ids) before retrieving anything, so the user confirms you're reporting on the right test. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Identify the target test (two entry paths)

- **A `test_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a test *name*** → resolve *top-down* first (account → workspace → project), applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

**Multiple tests:** for a small multi-test report, resolve each target test this way, confirm the full set, and run the Step 2 engine invocation **once per confirmed test** (the set is the user's pick, not a data-driven fan-out). Keep the per-test context so the report can label which run came from which test. For a whole suite, hand off to `bzm-portfolio-report` instead.

### Step 0b — Resolve the full hierarchy upward and confirm

Chain these calls — each response provides the ID needed for the next:

```
1. blazemeter_tests read         { test_id: <id> }
   → captures: test name, project_id, and the test's failure_criteria
     (failure_criteria.rules[] + failure_criteria.meta.* labels — keep these for the SLA section)

2. blazemeter_project read       { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read    { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read       { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

Display the resolved context before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a project_id missing from the test response), **stop and report the gap**. Once confirmed, carry the account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Resolve the window

Ask the user for the **time window** (e.g. "last 30 days", "since the 1.4 release") if they haven't said; default to the **last 30 days** ending now. Compute a concrete `[from, to]` timestamp pair and **display it** alongside the context block so the user can see exactly which runs are in scope. If the window turns out to contain fewer than two evaluable runs (Step 3), say so — a "trend" needs at least two — and offer to widen the window rather than fabricating one.

## Step 2 — Pull the run history with the engine (one invocation, the sole bulk source)

One engine invocation does the whole bulk pull and all the deterministic judgment — listing the test's executions, keeping runs that overlap the window, bucketing them by verdict, fetching each complete run's summary KPIs, resolving the baseline, and computing each run's deltas against it:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py history \
  --test-id <test_id> \
  --from 2026-06-01T00:00:00Z --to 2026-07-01T00:00:00Z \
  --baseline-file .blazemeter/baseline.json \   # only if the user's repo has one
  --pins <scratch>/pins.json \                  # only if the user pinned a baseline this conversation
  --out <scratch>/history.json
```

- The engine reads the **same credentials the MCP uses** from the environment — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file). Never pass keys on the command line. If it exits with a credentials error, show the user which variables to set and stop.
- **`--baseline-file`** — pass the user's committed `.blazemeter/baseline.json` when the repo has one; an entry for this test pins its baseline.
- **`--pins`** — if the user pinned a baseline for this test earlier **in this conversation**, write it as a small JSON map `{"<test_id>": "<execution_id>"}` to a scratch file and pass it. Pins outrank the committed file. Omit otherwise.
- Baseline precedence is applied inside the engine: **conversational pin → committed file → last passing run** (from the test's own history, which may legitimately predate the window). A test with no passing run gets `"source": "none"` — no baseline is invented.
- Stdout is a **short summary** (runs in window, pass/fail, baseline, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out`, and that file is the **sole source** for every cross-run number in the report.
- **Exit codes:** `0` success; `2` usage/credentials (tell the user what to set/fix); `3` scope-level failure (e.g. a bad test id) **or** too many fetch failures (default threshold 20%, tune with `--max-failure-rate`). On `3` the JSON may still exist — its `coverage` block says exactly what's missing; label the report **partial**, never complete.

## Step 3 — Map the history JSON into the Report data model

Read `--out`. It is compact — one entry per run in the window, already **oldest first** (the trend axis; keep that order in `runs[]`). Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. You get: top-level counts (`runs_in_window`, `kpi_runs`, `passed`/`failed`, `skipped_partial`, `inconclusive`, `still_running`), `baseline` (`source`: `pin | file | last-passing | none`, and the execution id), `baseline_kpis`, `candidate_execution_id`, `regressed_runs`, per-run entries, `incident_candidates`, `notes`, and `coverage`.

### 3a. Runs — the trend series

Include every run whose `bucket` is `kpi` as a `runs[]` row (partial/inconclusive/running runs stay out — report their counts in the coverage notes instead). The field names differ between the history JSON and the data model, so map explicitly:

| History-JSON field (per `runs[]` entry) | Data-model field (`runs[]` row) |
| --- | --- |
| `execution_id` | `execution_id` |
| `ended` (epoch **seconds**) | `timestamp` (**convert** to ISO 8601 UTC, e.g. `2026-06-24T15:36:00Z`) |
| — | `label` (a short date you derive from `timestamp`, e.g. `Jun 24 15:36`) |
| `report_status` | `status` |
| `kpis.avg_ms` | `kpis.avg_rt_ms` |
| `kpis.p90_ms` | `kpis.p90_ms` |
| `kpis.p95_ms` | `kpis.p95_ms` |
| `kpis.p99_ms` | `kpis.p99_ms` |
| `kpis.throughput_rps` | `kpis.rps` |
| `kpis.error_rate_pct` | `kpis.error_rate_pct` |
| `kpis.max_users` | `kpis.concurrency` |

A run noted `kpis_unavailable` (some run types, e.g. GUI/EUX, report no load KPIs) keeps its row with `kpis: {}` — the template renders dashes, never fabricated zeros.

### 3b. Regressions — from the engine's deltas

Each non-baseline KPI run carries `deltas` vs the baseline (`avg`, `p95`, `p99`, `throughput`, `error_rate` — each with a signed `pct` and an `adverse` flag; adverse means a ≥10% move in the worse direction, and throughput is judged per-virtual-user when the load config changed, flagged `normalized_per_vu`), plus `worst_kpi_move` and a `regressed` flag. Build one `regressions[]` entry per **adverse** delta on the runs that matter (at minimum every `regressed` run; lead with the candidate):

- `kpi` — a readable name (`avg` → `avg response time`, `p95` → `p95 response time`, `throughput` → `throughput`, `error_rate` → `error rate`).
- `from_value` / `to_value` — the baseline's KPI (top-level `baseline_kpis`) and the run's KPI, same units.
- `pct_change` — the delta's `pct` verbatim (an `error_rate` delta with `pct: null` carries `points` instead — put the points move in the `note`, e.g. "baseline was clean; run at 2.4%").
- `direction` — `up` for a latency/error rise, `down` for a throughput fall (for latency/error a rise is bad; for throughput a fall is bad).
- `severity` — `critical` when the run also failed (`report_status: fail`) or the move breaches an SLA threshold; `warning` for other adverse moves; `info` for context-only entries.
- `run_id` / `note` — the run's `execution_id`; note `normalized_per_vu` throughput comparisons ("per-VU — load config changed") and anything from `incident_candidates` (e.g. an `error_spike` at ≥1%, `severe` at ≥5%).

If `baseline.source` is `none` (or the notes say `baseline_kpis_unavailable`), leave `regressions` empty and say in the narrative that no baseline comparison was possible. `baseline_is_only_run` in the notes means a pinned baseline points at the newest run itself — report "baseline run, no prior to compare", not a 0% move.

### 3c. SLA — counts from the history, rules from the test

- `pass_count` / `fail_count` — the top-level `passed` / `failed` verbatim.
- `rules[]` — describe the rules from the **test's `failure_criteria`** (Step 0b's `blazemeter_tests read`) using its `meta.general_labels` / `meta.rule_field_labels` / `meta.kpi_labels` / `meta.condition_labels` — **never raw kpi ids or op codes**. No per-criterion per-run result exists, so attribute a failing run to a rule by comparing that run's mapped KPIs against the rule's threshold (e.g. an "error rate % > 4" rule vs a run at 26.7% → implicated) and compute each rule's `pass_rate_pct` from that inference — and say it's an inference in the `note`.

### 3d. Endpoint hot spots — one MCP drill-in on the latest run

The per-endpoint breakdown is a **single-run drill-in**, so it stays on the MCP: call `blazemeter_execution read_all_reports` for the **latest** run (the history's `candidate_execution_id`) and use its `request_stats[]`. Rank labels by `percentile_95_ms` and by error contribution (`errors_count` share of total, but surface any low-traffic label with a near-100% `errors_rate_percent`). Map `label_name`→`name`, `percentile_95_ms`→`p95_ms`, `errors_rate_percent`→`error_rate_pct`; set `trend` only if you have evidence (an `endpoint_error_spike` incident candidate names a label erroring at near-100% — mark it `degrading`), else omit it. Do **not** page endpoint stats across all runs — that whole-history drill-in is exactly the fan-out the engine exists to avoid, and the report's endpoint table is a snapshot of the latest run.

## Step 4 — Assemble the Report data model (JSON)

Build a single JSON object matching the Report data model (authoritative shape: the structure below — `meta` / `summary` / `runs` / `regressions` / `sla` / `endpoints`). **Supply `generated_at` yourself** (the current time, ISO 8601) — the template never reads the clock, so the render is deterministic. Put **no credentials** anywhere in the model.

```json
{
  "meta": {
    "title": "<test name> — Cross-Run Trend & Regression",
    "subtitle": "<N> runs over <window>",
    "generated_at": "<ISO 8601 now>",
    "window_start": "<ISO date>", "window_end": "<ISO date>",
    "context": {
      "account":   { "name": "<account name>",   "id": "<account_id>" },
      "workspace": { "name": "<workspace name>", "id": "<workspace_id>" },
      "project":   { "name": "<project name>",   "id": "<project_id>" },
      "test":      { "name": "<test name>",      "id": "<test_id>" }
    }
  },
  "summary": {
    "verdict": "SHIP | NO-SHIP | REGRESSED | STABLE",
    "headline": "<one-line takeaway>",
    "narrative": ["<2-4 short paragraphs of expert assessment>"]
  },
  "runs": [
    { "execution_id": "<id>", "timestamp": "<ISO>", "label": "<short date>", "status": "<report_status>",
      "kpis": { "avg_rt_ms": 0, "p90_ms": 0, "p95_ms": 0, "p99_ms": 0, "rps": 0, "error_rate_pct": 0, "concurrency": 0 } }
  ],
  "regressions": [
    { "kpi": "p95 response time", "from_value": 0, "to_value": 0, "pct_change": 0,
      "direction": "up", "severity": "critical", "run_id": "<id>", "note": "<why>" }
  ],
  "sla": {
    "pass_count": 0, "fail_count": 0,
    "rules": [ { "label": "<readable rule>", "threshold": "<e.g. > 2000 ms>", "pass_rate_pct": 0, "note": "<…>" } ]
  },
  "endpoints": [
    { "name": "<label>", "p95_ms": 0, "error_rate_pct": 0, "trend": "degrading | stable | improving" }
  ]
}
```

`meta.context` comes from Step 0; `window_start`/`window_end` from Step 1; `runs`/`regressions`/`sla` from Step 3's mappings; `endpoints` from Step 3d. Your contribution is the `summary` block — verdict, headline, and a short expert narrative grounded in the engine's numbers (mention the baseline source and id, `regressed_runs`, and any coverage gaps). Omit a section by leaving its array empty (or `sla` absent) — the template renders a tidy "none" state. Keep the JSON ready to inject in Step 5 (write it to a scratch working file, or hold it inline).

## Step 5 — Emit the branded Report (template fill, no interpreter)

The Report is a single shipped HTML template that renders itself from the data model in the browser — there is **no Python step and no local interpreter**. Produce the file with three deterministic actions:

1. **Read the template** at `${CLAUDE_PLUGIN_ROOT}/skills/bzm-report/assets/report-template.html`. It bakes in the CSS, the approximated-BlazeMeter brand vars, and the vendored client-side JS that builds every section (context, summary, run history, regressions, SLA, endpoints, and the trend charts derived from `runs[]`).
2. **Serialize your Step 4 data model to JSON** and **replace the single token `{{REPORT_DATA_JSON}}`** with it. The token sits inside `<script>window.REPORT_DATA = {{REPORT_DATA_JSON}};</script>`, so before substituting, **HTML-escape every `</` in the JSON to `<\/`** — that is the one transform that guarantees a string value (e.g. an endpoint label like `</checkout>`) can never close the `<script>` tag early. Substitute the token literally; do not otherwise reformat the template.
3. **Write the result** as a `.html` file (default `./bzm-reports/`, filename a slug of the test name + `generated_at`). Use the `Write` tool — no shell, no `python`.

The output is fully self-contained (offline, no CDN — safe to email): the same single token is the only thing that varies run-to-run, so layout and branding stay deterministic. To re-brand later, edit the CSS `:root` vars (and the inline logo SVG) in the template; that is a template edit, not a code change.

## Step 6 — Drill-ins stay interactive

When the user asks about one run after seeing the report ("what happened in run 82525951?", "which endpoints were slow there?"), that is a *single-run* question — answer it with the MCP (`blazemeter_execution read`, `read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure` for a failed run). Don't re-run the history pull for it, and don't page MCP reports across many runs — that's what Step 2 was for.

## Output template

After rendering, tell the user:

```
## BlazeMeter Report: <test name>
**Window:** <start> – <end>   |   **Runs:** <N> (<skipped_partial> partial skipped)
**Baseline:** <source> <execution_id>   |   **Verdict:** <SHIP / NO-SHIP / REGRESSED / STABLE>
**Report file:** <path to the HTML>

### Highlights
- <top regression or trend, with numbers>
- <SLA compliance: N/K runs passed>
- <worst endpoint hot spot>

### Coverage notes                                  ← only when something is missing
- Fetch coverage: <ok>/<attempted> (<failed> failed) — report is partial
- Skipped (aborted/errored) runs: <N> — KPIs not folded in

Open the HTML file to see the full branded report (trend charts, run history, regressions, SLA, endpoints).
```

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_execution list` + per-run report reads burns enormous time and tokens on a busy test and is exactly what the engine exists for. MCP is for Step 0's interactive picks, the consent gate, the test object's SLA rules, and single-run drill-ins (Step 3d, Step 6) — nothing in between.
- **Consent before pull.** The AI-consent check (Step 0b) must pass before any `history` invocation — the gate lives in the MCP layer, and the engine assumes it already happened.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the data model, or in the generated HTML (it's meant to be emailed — keep it secret-free).
- **Trust the engine's arithmetic.** Deltas, normalization, baseline choice, and status buckets are computed deterministically and fixture-tested. Your job is the mapping, the narrative, and the severity judgment — if a number looks wrong, say so and show it; don't silently recompute or re-fetch.
- **Field-name mapping is exact.** The history JSON says `avg_ms` / `throughput_rps` / `max_users`; the data model says `avg_rt_ms` / `rps` / `concurrency`. Map deliberately (the Step 3a table) — a mis-key silently drops a KPI from the charts.
- **Timestamps need converting.** The history JSON's `started`/`ended` are epoch **seconds**; the template's `timestamp` is an ISO 8601 string. Convert, or every run label renders as the raw execution id.
- **The engine already excludes partial runs.** Aborted/errored runs are counted (`skipped_partial`) but their KPIs never fold into the trend — including them would distort it. Report the count in the coverage notes.
- **`kpis_unavailable` is not a zero.** Some run types (e.g. GUI/EUX) report no load KPIs at all — such a run keeps its row with an empty `kpis` object (dashes in the table), never a 0 ms / 0% row.
- **Don't compare a run to itself.** Last-passing resolution excludes the candidate, so a still-green regression is detectable whenever any prior pass exists. `baseline_is_only_run` in the notes means a *pinned* baseline points at the newest run itself — report "baseline run, no prior to compare", not a 0% move. The baseline run's own row carries `is_baseline` and no deltas.
- **Load-config drift.** If `max_users` differs across runs, raw throughput isn't apples-to-apples — the engine's deltas already switch to RPS-per-VU (`normalized_per_vu`); say so wherever you surface such a throughput comparison.
- **Deep windows can hit the history cap.** The engine pages executions newest-first (50 per page, up to 20 pages ≈ 1,000 runs) and stops once a page predates the window. A hyperactive CI test can have its **oldest in-window runs truncated** — if `runs_in_window` looks suspiciously flat for a very active test, say the history may be capped and offer a shorter window.
- **Failure-criteria labels come from the test, not the execution.** Describe SLA rules with the test object's `failure_criteria.meta.*` labels (Step 0b); the runs only carry the overall `report_status`, and there is no per-criterion per-run result array — attribute failures by comparing KPIs to thresholds, and say it's an inference.
- **`generated_at` is supplied, not read.** The template never reads the clock (so the render is deterministic). You provide the current timestamp; `meta.title` and `meta.generated_at` are required — omit them and the header renders blank.
- **Escape `</` before substituting.** The data model is injected into a `<script>` tag, so any `</` inside a string value (an endpoint label, a narrative line) must become `<\/` first — otherwise it can close the tag early and break the report. This is the only transform the JSON needs.
- **The template is the source of layout/branding.** Don't hand-write report HTML or build sections yourself — always fill `assets/report-template.html` so every report is consistent and on-brand. Its client-side JS builds the sections and charts from `window.REPORT_DATA` at open time; output is self-contained and offline by design.
- **Never persist scope.** The resolved account/workspace/project/test is conversational memory only. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing. Scratch files (`pins.json`, `history.json`, the working data model) go in the session scratch directory, not the user's repo; only the final `.html` lands where the user asked.
