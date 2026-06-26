---
name: triage-blazemeter-failure
description: Deep-dive a single failed or regressed BlazeMeter execution and produce a prioritized triage report. Use when a specific run failed its criteria, regressed, or threw errors and you need an error/endpoint/anomaly breakdown plus actionable next steps for a developer or SRE.
---

Triage one failed or regressed BlazeMeter execution: break errors down by type **and** by endpoint, rank endpoint hot spots by p95 latency and error contribution, summarize anomalies, separate likely-**systemic** problems from one-off **noise**, and end with prioritized, actionable next steps an engineer can act on.

This skill diagnoses a **single execution** in depth. It is the counterpart to `analyze-blazemeter-test`, which trends *many* runs of a test over time — reach for that when you need history, for this when you need to understand *why one run went wrong*.

## Step 0 — Resolve and confirm context (account → workspace → project → test → execution)

This is the canonical Context Resolution step from `shared/conventions.md` §4, extended one level down to the **execution** under triage. Always resolve and **display** the full context (with ids) before pulling any reports, so the user can confirm you're diagnosing the right run. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests/executions, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Identify the target execution (entry paths)

- **An `execution_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **A `test_id` was given but no execution** → resolve the test's hierarchy (Step 0b), then list its runs and pick the failed/regressed one to triage:
  - `blazemeter_execution list { test_id: <id>, limit: 50, offset: 0 }`, paging by 50.
  - Prefer the run the user named; otherwise present the most recent runs (with `execution_id`, end time, and `execution_status`) and let the user pick the failed/regressed one. **Don't silently grab the latest** — the latest run may be green while the run worth triaging is older.
- **Nothing, or only a test *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, presented as a confirmable/overridable suggestion; if a level has exactly one option, just display it.
  - To enumerate, list one page (`limit: 50`). **Small set** (page not full) → numbered list, each entry with its id, user picks. **Too big to list** (page comes back full) → don't dump it; ask the user to name or paste the workspace/project/test/execution (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*, then list its executions as above.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

### Step 0b — Resolve the full hierarchy upward and confirm

Regardless of how the execution was identified, always resolve and display its full organizational context before proceeding. Chain these calls — each response provides the ID needed for the next:

```
1. blazemeter_execution read     { execution_id: <id> }
   → captures: execution status, ended timestamp, test_id

2. blazemeter_tests read         { test_id: <test_id from step 1> }
   → captures: test name, project_id

3. blazemeter_project read       { project_id: <project_id from step 2> }
   → captures: project name, workspace_id

4. blazemeter_workspaces read    { workspace_id: <workspace_id from step 3> }
   → captures: workspace name, account_id

5. blazemeter_account read       { account_id: <account_id from step 4> }
   → captures: account name, AI-consent state
```

**AI Consent gate:** if the account has **not** enabled AI consent (from step 5), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into triage.

Present the resolved context to the user before continuing:

```
Execution:  <execution_id>  (status: <execution_status>, ended: <ended timestamp>)
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a `test_id` or `project_id` is missing from a response), **stop and report the gap** — do not proceed against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

### Step 0c — Confirm the run is actually finished and worth triaging

From the `blazemeter_execution read` in step 0b:

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

Also reuse the `blazemeter_execution read` response from Step 0b for the **failure-criteria results** (the per-criterion pass/fail that explains the `execution_status` verdict).

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
- **By error contribution:** rank endpoints by their share of total errors (error count × ... weight toward absolute count, but surface a low-traffic endpoint with a near-100% error rate too — it may be fully broken).
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

### 2e. Failure-criteria explanation
From the `blazemeter_execution read` failure criteria:
- List exactly **which criteria failed** and by how much (actual vs. threshold), so the `execution_status` verdict is explained, not just stated.
- When presenting criteria, use the label fields — `meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels` — **never** raw kpi ids or op codes.

## Step 3 — Deliver the triage report

Structure the output as:

```
## BlazeMeter Failure Triage: <test name> — execution <execution_id>
**Status:** <execution_status>  |  **Ended:** <ended timestamp>  |  **Account:** <account name> (<account_id>)

### Verdict
2–3 sentences: what failed, the single most likely root cause, and whether it looks systemic or like noise.

### Failure Criteria
- Failed criteria: <criterion (actual vs threshold)> ...
- (or: "No criteria defined — status is `unset`, no SLA verdict")

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
- **Failure-criteria labels:** use `meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels` when presenting criteria — never raw kpi ids or op codes.
- **MCP-first:** every step here is a `blazemeter_execution` MCP action; no REST v4 fallback is needed for single-execution triage. If a future report field is missing from the MCP, that — and only that — would justify a documented REST v4 call.
- **Parallel fetches:** `read_errors`, `read_request_stats`, `read_anomalies_stats`, and `read_summary` are independent for one execution — call them in parallel.
