---
name: blazemeter-report
description: Generate a branded, self-contained HTML cross-run trend & regression Report for a BlazeMeter test over a time window — trend lines, regression flags, and SLA compliance across many runs. Use when asked for a shareable/stakeholder report, a release report, a multi-run trend or regression summary, or a portfolio/scorecard view the platform's single-run reports can't produce.
---

Produce the flagship **Report**: retrieve many runs of a test (or a few tests) over a window, shape them into the **Report data model**, and emit a branded, self-contained HTML file that surfaces trends, regressions, and SLA compliance across the window — the cross-execution/time view BlazeMeter's single-run reports can't give you.

This skill **retrieves and normalizes**, then fills a shipped HTML template. You build a Report data model (JSON) and drop it into a single static, self-contained template (`assets/report-template.html`); the template's baked-in CSS and vendored client-side JS own the layout, branding, and charts (it builds every section — context, summary, run history, regressions, SLA, endpoints, and the trend charts — from the data at open time). No local interpreter is involved: the render is a token replacement plus a file write, so the skill runs identically across the CLI, VS Code, and the desktop app.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

This is the canonical Context Resolution step from `shared/conventions.md` §4. Always resolve and **display** the full context (with ids) before retrieving anything, so the user confirms you're reporting on the right test. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Identify the target test (two entry paths)

- **A `test_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a test *name*** → resolve *top-down* first (account → workspace → project), applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, presented as a confirmable/overridable suggestion; if a level has exactly one option, just display it.
  - To enumerate, list one page (`limit: 50`). **Small set** (page not full) → numbered list, each entry with its id, user picks. **Too big to list** (page comes back full) → don't dump it; ask the user to name or paste the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

**Multiple tests:** for a small multi-test report, resolve each target test this way and confirm the full set before retrieving. Keep the per-test context so the report can label which run came from which test.

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

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding.

Display the resolved context before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a project_id missing from the test response), **stop and report the gap**. Once confirmed, carry the account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Establish the window and select the executions

Ask the user for the **time window** (e.g. "last 30 days", "since the 1.4 release") if they haven't said. Then list the test's executions and select the ones in scope:

```
blazemeter_execution list  { test_id: <id>, limit: 50, offset: 0 }
```

- Page with `offset` in steps of 50 until you have every execution in (or overlapping) the window.
- Keep only **finished, evaluable** runs: an execution is finished only when its **`ended` is not null** (confirm via `blazemeter_execution read` if the list entry is ambiguous). **Skip** `aborted` / `error` / `noData` runs — they have incomplete data that distorts trends; note how many you skipped.
- Sort the kept runs **chronologically (oldest → newest)** — the trend direction depends on it.
- If only one run falls in the window, say so: a "trend" needs at least two; offer to widen the window.

Display the selected runs (id, end time, status) and confirm the set before the (potentially many) report calls.

## Step 2 — Retrieve each run's data

For each selected execution, call these — they are independent per execution, so **fetch in parallel**:

```
blazemeter_execution read              { execution_id: <id> }   # execution_status + ended timestamp
blazemeter_execution read_all_reports  { execution_id: <id> }   # summary + errors + request_stats
```

Map the **summary** report's `overall_metrics` into the data model's KPI fields — the field names differ, so map explicitly:

| Report field (`overall_metrics`) | Data-model KPI (`kpis`) |
| --- | --- |
| `average_response_time_ms` | `avg_rt_ms` |
| `percentile_90_ms` | `p90_ms` |
| `percentile_95_ms` | `p95_ms` |
| `percentile_99_ms` | `p99_ms` |
| `average_throughput_per_second` | `rps` |
| `error_rate_percent` | `error_rate_pct` |
| `max_concurrent_users` | `concurrency` |

Take each run's `status` from `blazemeter_execution read` → `execution_status`, and its `timestamp` from that response's `ended` (or `created`).

For **endpoint hot spots**, use the **latest** run's `read_all_reports` → `request_stats[]`: rank labels by `percentile_95_ms` and by error contribution (`errors_count` share of total, but surface any low-traffic label with a near-100% `errors_rate_percent`). Map `label_name`→`name`, `percentile_95_ms`→`p95_ms`, `errors_rate_percent`→`error_rate_pct`.

## Step 3 — Compute trends, regressions, and SLA compliance

- **Trend:** the per-run KPI series *is* the trend (the engine draws the charts from `runs[]`). No extra work beyond ordering.
- **Regressions:** for each KPI, compare the latest run to the prior run (and/or to a rolling baseline). Flag any KPI that moved **≥ a threshold** (default **10%**; let the user override). Record `from_value`, `to_value`, signed `pct_change`, `direction` (`up`/`down`), and a `severity` (`critical` for SLA-breaching or large moves, `warning` for moderate, `info` otherwise). For latency/error a rise (`up`) is bad; for throughput a fall (`down`) is bad.
- **Normalize for load-config differences:** if concurrency differs across runs (compare `concurrency` / `max_concurrent_users`), raw throughput isn't apples-to-apples — note it, and prefer RPS-per-VU when comparing.
- **SLA compliance:** count runs whose `execution_status` is `pass` vs `fail`. Describe the rules from the **test's `failure_criteria`** (Step 0b's `blazemeter_tests read`) using its `meta.general_labels` / `meta.rule_field_labels` / `meta.kpi_labels` / `meta.condition_labels` — **never raw kpi ids or op codes**. The MCP exposes no per-criterion per-run result, so attribute a failing run to a rule by comparing that run's summary KPIs to the rule's threshold.

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
    { "execution_id": "<id>", "timestamp": "<ISO>", "label": "<short date>", "status": "<execution_status>",
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

Omit a section by leaving its array empty (or `sla` absent) — the template's client-side JS renders a tidy "none" state. Keep the JSON ready to inject in Step 5 (write it to a working file, or hold it inline).

## Step 5 — Emit the branded Report (template fill, no interpreter)

The Report is a single shipped HTML template that renders itself from the data model in the browser — there is **no Python step and no local interpreter**. Produce the file with three deterministic actions:

1. **Read the template** at `${CLAUDE_PLUGIN_ROOT}/skills/blazemeter-report/assets/report-template.html`. It bakes in the CSS, the approximated-BlazeMeter brand vars, and the vendored client-side JS that builds every section (context, summary, run history, regressions, SLA, endpoints, and the trend charts derived from `runs[]`).
2. **Serialize your Step 4 data model to JSON** and **replace the single token `{{REPORT_DATA_JSON}}`** with it. The token sits inside `<script>window.REPORT_DATA = {{REPORT_DATA_JSON}};</script>`, so before substituting, **HTML-escape every `</` in the JSON to `<\/`** — that is the one transform that guarantees a string value (e.g. an endpoint label like `</checkout>`) can never close the `<script>` tag early. Substitute the token literally; do not otherwise reformat the template.
3. **Write the result** as a `.html` file (default `./blazemeter-reports/`, filename a slug of the test name + `generated_at`). Use the `Write` tool — no shell, no `python`.

The output is fully self-contained (offline, no CDN — safe to email): the same single token is the only thing that varies run-to-run, so layout and branding stay deterministic. To re-brand later, edit the CSS `:root` vars (and the inline logo SVG) in the template; that is a template edit, not a code change.

## Output template

After rendering, tell the user:

```
## BlazeMeter Report: <test name>
**Window:** <start> – <end>   |   **Runs:** <N> (<skipped> skipped)
**Verdict:** <SHIP / NO-SHIP / REGRESSED / STABLE>
**Report file:** <path to the HTML>

### Highlights
- <top regression or trend, with numbers>
- <SLA compliance: N/N runs passed>
- <worst endpoint hot spot>

Open the HTML file to see the full branded report (trend charts, run history, regressions, SLA, endpoints).
```

## Gotchas

- **Field-name mapping is exact.** The summary report uses `average_response_time_ms` / `average_throughput_per_second` / `error_rate_percent` / `percentile_9X_ms` / `max_concurrent_users`; the data model uses `avg_rt_ms` / `rps` / `error_rate_pct` / `p9X_ms` / `concurrency`. Map deliberately (Step 2) — a mis-key silently drops a KPI from the charts.
- **`generated_at` is supplied, not read.** The template never reads the clock (so the render is deterministic). You provide the current timestamp; `meta.title` and `meta.generated_at` are required — omit them and the header renders blank.
- **Completion = `ended != null`.** Skip `aborted` / `error` / `noData` runs; including them distorts the trend. Note how many you skipped.
- **Pagination.** `blazemeter_execution list` maxes at 50 per call — page with `offset` until the window is covered.
- **Failure-criteria labels come from the test, not the execution.** Describe SLA rules with the test object's `failure_criteria.meta.*` labels (Step 0b); the execution only carries the overall `execution_status`, and there is no per-criterion per-run result array — attribute failures by comparing KPIs to thresholds.
- **Load-config drift.** If `concurrency` varies across runs, raw throughput isn't apples-to-apples — say so and prefer RPS-per-VU.
- **No credentials in the model.** The model holds data + narrative only; Platform Credentials never belong in it (the template only ever sees the data you inject).
- **Escape `</` before substituting.** The data model is injected into a `<script>` tag, so any `</` inside a string value (an endpoint label, a narrative line) must become `<\/` first — otherwise it can close the tag early and break the report. This is the only transform the JSON needs.
- **The template is the source of layout/branding.** Don't hand-write report HTML or build sections yourself — always fill `assets/report-template.html` so every report is consistent and on-brand. Its client-side JS builds the sections and charts from `window.REPORT_DATA` at open time; output is self-contained and offline by design.
