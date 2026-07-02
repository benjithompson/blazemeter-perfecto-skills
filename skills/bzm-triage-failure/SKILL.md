---
name: bzm-triage-failure
description: Deep-dive a single failed or regressed BlazeMeter execution and produce a prioritized triage report. Use when a specific run failed its criteria, regressed, or threw errors and you need an error/endpoint/anomaly breakdown plus actionable next steps for a developer or SRE.
---

Triage one failed or regressed BlazeMeter execution: break errors down by type **and** by endpoint, rank endpoint hot spots by p95 latency and error contribution, summarize anomalies, separate likely-**systemic** problems from one-off **noise**, and end with prioritized, actionable next steps an engineer can act on.

This skill diagnoses a **single execution** in depth. It is the counterpart to `bzm-test-analysis`, which trends *many* runs of a test over time — reach for that when you need history, for this when you need to understand *why one run went wrong*.

## Step 0 — Resolve and confirm context (account → workspace → project → test → execution)

Context resolution here extends one level down to the **execution** under triage. Always resolve and **display** the full context (with ids) before pulling any reports, so the user can confirm you're diagnosing the right run. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests/executions, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Identify the target execution (entry paths)

- **An `execution_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **A `test_id` was given but no execution** → resolve the test's hierarchy (Step 0b), then list its runs and pick the failed/regressed one to triage:
  - `blazemeter_execution list { test_id: <id>, limit: 50, offset: 0 }`, paging by 50.
  - Prefer the run the user named; otherwise present the most recent runs (with `execution_id`, end time, and `execution_status`) and let the user pick the failed/regressed one. **Don't silently grab the latest** — the latest run may be green while the run worth triaging is older.
- **Nothing, or only a test *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test/execution (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching — except a **test** name, which resolves with one `blazemeter_tests search` (`test_name` + `account_id`, scoped to the already-confirmed levels via `workspace_id_list`/`project_id_list` — never unscoped across the account; pass a full-history `custom` window, e.g. `start_time` 2000-01-01 → today, since the default `time_frame` only matches tests created today; results come 50/page — if `has_more` is true, page on or ask for a narrower fragment) (executions have no usable name search — execution names are just the test'\''s display name, and search rows carry no test id or status; resolve a run via `blazemeter_execution list` within the confirmed test)).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*, then list its executions as above.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

### Step 0b — Resolve the full hierarchy upward and confirm

Regardless of how the execution was identified, always resolve and display its full organizational context before proceeding. Chain these calls — each response provides the ID needed for the next:

```
1. blazemeter_execution read     { execution_id: <id> }
   → captures: execution_status, ended timestamp, project_id, execution_name
     (execution_name is the test's display name; note the execution API does NOT
      expose test_id, so there is no tests-read hop on this path)

2. blazemeter_project read       { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read    { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read       { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into triage.

Present the resolved context to the user before continuing:

```
Execution:  <execution_id>  (status: <execution_status>, ended: <ended timestamp>)
Test:       <execution_name>  (test_id not exposed by the execution API)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If a link in the chain fails — a `project_id`, `workspace_id`, or `account_id` is missing from a response, or a `read` returns 403 — **stop and report the gap** rather than proceeding against an unverified context. (Do **not** stop because `test_id` is absent: the execution API never returns one, so it's always missing here. If the user wants test-level enrichment — configured load, failure-criteria thresholds — ask them for the `test_id`.) Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

### Step 0c — Confirm the run is actually finished and worth triaging

From the `blazemeter_execution read` in step 0b (which carries `execution_status` and `ended`):

- **Completion:** the run is finished only when the **`ended` field is NOT null**. If `ended == null`, the run is still in progress — stop and tell the user to re-run triage once it ends, rather than diagnosing partial data.
- **Status sanity-check** — `execution_status` ∈ `pass | fail | unset | abort | error | noData`:
  - `fail` / `abort` / `error` → the expected triage targets; proceed.
  - `unset` → the run defined **no failure criteria**, so pass/fail is *indeterminate*, not a success. Say so; you can still triage errors and anomalies, but there is no SLA verdict to explain.
  - `noData` → the run produced no usable samples (often a load-generator or connectivity failure, not an application bug). Report that and check the run's own logs rather than mining endpoint stats that don't exist.
  - `pass` → confirm with the user that they really want a deep-dive on a passing run (e.g. a *regression* that still squeaked under criteria) before spending the calls.

## Step 1 — Pull the diagnostic reports

For the confirmed `execution_id`, fetch these report actions. They are **independent — call them in parallel** to keep wall-clock time down:

```
blazemeter_execution read_errors          { execution_id: <id> }   # errors by type & by endpoint
blazemeter_execution read_request_stats   { execution_id: <id> }   # per-endpoint (label) KPIs
blazemeter_execution read_anomalies_stats { execution_id: <id> }   # anomaly detection status
blazemeter_execution read_summary         { execution_id: <id> }   # aggregate KPIs for the run
```

The `execution_status` from Step 0b's `blazemeter_execution read` is the **overall pass/fail verdict** for the run. Note the per-criterion thresholds and their readable labels are **not** on the execution — they live on the test object (reachable only with a `test_id`, which the execution API doesn't expose). Explain the verdict from the run's KPIs (Step 2e); if the user supplies the `test_id`, `blazemeter_tests read` can enrich it with exact thresholds.

What each report gives you:
- **read_errors** — error breakdown by type/response-code and by endpoint (label): counts and the responses' messages.
- **read_request_stats** — per-endpoint KPIs: samples, avg / p90 / p95 / p99 response time, error count and error %, throughput.
- **read_anomalies_stats** — `anomaly_detection_status` ∈ `no_anomalies | anomalies_with_details | statistics_unavailable`, plus per-anomaly detail (KPI, window) when present.
- **read_summary** — run-level aggregate KPIs (avg/p95/p99 RT, RPS, overall error rate, concurrency) — the denominator the per-endpoint numbers are weighed against.

## Step 2 — Triage the run

Work through these lenses in order; each builds on the previous.

### 2a. Error breakdown — by type and by endpoint
From `read_errors`:
- **By type:** group errors by response code / error message; tabulate count and % of total errors. Call out the dominant failure mode (e.g. `503` vs `Connection reset` vs assertion failures behave very differently).
- **By endpoint:** for each label, list its error count and which error types it produced. An error confined to one label points at that handler; the same error spread across many labels points at a shared dependency (DB, auth, downstream service, the load generator itself).

### 2b. Endpoint hot-spot ranking
From `read_request_stats`:
- **By p95 (latency):** rank endpoints by p95 response time, slowest first — these are where users feel pain.
- **By error contribution:** rank endpoints by their share of total errors — each endpoint's `errors_count / total_errors`. **Also** surface any low-traffic endpoint with a near-100% `errors_rate_percent`, even if its absolute count is small — it may be fully broken while hiding behind a low sample count.
- Cross-reference the two rankings. An endpoint that tops **both** lists is the strongest single lead.

### 2c. Anomaly summary
From `read_anomalies_stats`, interpret the status (do not treat all three the same):
- `anomalies_with_details` → real anomalies; list each with its KPI and time window.
- `no_anomalies` → the engine ran and found nothing — a genuine clean signal.
- `statistics_unavailable` → the run was **too short for the engine to build a baseline**. This is **insufficient data, not a finding** — never report it as "anomalies detected" or as a clean run.

### 2d. Systemic vs. noise
Synthesize 2a–2c into a judgment for each notable signal:
- **Likely SYSTEMIC** — recurs across multiple endpoints, correlates with an anomaly window, tracks a shared dependency, or scales with load (error rate climbs as concurrency ramps). These warrant a fix.
- **Likely NOISE / one-off** — a single isolated sample, a lone transient timeout, an error on one low-traffic label with no anomaly and no spread. Name it explicitly as probable noise so the team doesn't chase it.
- When the evidence is thin (e.g. `statistics_unavailable`, or a `noData` / `unset` run), say the verdict is **inconclusive** rather than forcing a systemic/noise call.

### 2e. Explain the verdict
- State the overall `execution_status` (from Step 0b) as the run's verdict — `fail`, `abort`, `error`, etc.
- Explain **why it likely failed** from the run's own KPIs: point at the metrics most likely to have tripped criteria — e.g. `error_rate_percent` from `read_summary`, or a per-endpoint `percentile_95_ms` / `errors_rate_percent` from `read_request_stats` — and say which look out of line.
- Be explicit that this is an **inference from observed KPIs**, not a read of the configured criteria. The exact failure-criteria thresholds and their readable meta labels (`meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels`) live on the **test object** and are **not reachable from an execution alone** — there's no `test_id` on this path. If the user wants the criteria spelled out (actual vs. threshold, by name), ask them for the `test_id` so `blazemeter_tests read` can supply them.

## Step 3 — Deliver the triage report

Structure the output as:

```
## BlazeMeter Failure Triage: <execution_name> — execution <execution_id>
**Status:** <execution_status>  |  **Ended:** <ended timestamp>  |  **Account:** <account name> (<account_id>)

### Verdict
2–3 sentences: what failed, the single most likely root cause, and whether it looks systemic or like noise.

### Why It Failed
- Verdict: status is `<execution_status>`.
- Likely-violated thresholds (inferred from run KPIs): <e.g. overall error_rate_percent N%, /checkout p95 N ms> ...
- (or: "Status is `unset` — no failure criteria defined, so no SLA verdict")
- Exact criteria (thresholds + labels) live on the test object — not reachable from an execution alone; supply a `test_id` to enrich.

### Error Breakdown
**By type**
| Error type / code | Count | % of errors |
...
**By endpoint**
| Endpoint | Errors | Error types seen |
...

### Endpoint Hot Spots
| Endpoint | p95 (ms) | Error % | Samples | Notes |
...
(slowest-by-p95 and highest-error endpoints; flag any in both lists)

### Anomalies
- Status: <no_anomalies | anomalies_with_details | statistics_unavailable>
- Details: <KPI + window per anomaly, or "too short for a baseline" / "none">

### Systemic vs. Noise
- **Systemic:** <signals recurring across endpoints / anomalies>
- **Noise / one-off:** <isolated, transient items to ignore>

### Prioritized Next Steps
1. <P1 — highest-leverage, most-likely-systemic fix, with the endpoint/error that justifies it>
2. <P2 — ...>
3. <P3 — ...>
(each step concrete enough for a developer/SRE to pick up: what to look at, where, and why)
```

## Gotchas

- **Completion detection:** a run is finished only when the **`ended` field is NOT null** — `ended == null` means it's still running. Don't rely on status strings alone to decide it's done.
- **`unset` ≠ pass:** `execution_status: unset` means no failure criteria were defined, so there is **no** pass/fail verdict — report it as indeterminate, not green.
- **`noData` runs:** no usable samples — usually a load-generator/connectivity failure, not an app bug. Don't mine endpoint stats that don't exist; point the user at the run's own logs.
- **`statistics_unavailable` anomalies:** the run was too short for the anomaly engine to build a baseline — this is **insufficient data, not a signal**. Never present it as "no anomalies" or as anomalies found.
- **Absolute vs. relative error counts:** rank endpoints by both. A high-traffic label can dominate raw error counts while a low-traffic label sits at a near-100% error rate — surface the broken-but-quiet endpoint too.
- **One error across many endpoints = shared dependency:** the same error type spread across unrelated labels usually points at a shared layer (DB, auth, downstream service) or at the load generator — not at each endpoint individually.
- **Failure criteria aren't on the execution:** `blazemeter_execution read` returns only the overall `execution_status` — not the per-criterion thresholds or their readable meta labels. Those (`meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels`) live on the **test object**, reachable only with a `test_id`, which the execution API doesn't expose. Explain the verdict from the run's KPIs; ask for a `test_id` if the user wants the exact criteria.
- **MCP-first:** every step here is a `blazemeter_execution` MCP action; no REST v4 fallback is needed for single-execution triage. If a future report field is missing from the MCP, that — and only that — would justify a documented REST v4 call.
- **Parallel fetches:** `read_errors`, `read_request_stats`, `read_anomalies_stats`, and `read_summary` are independent for one execution — call them in parallel.
