---
name: bzm-analyze-test
description: Analyze BlazeMeter test execution results over time as a QA Performance engineer. Use when asked to analyze, review, trend, or report on a BlazeMeter test's performance history, regressions, error patterns, or SLA compliance.
---

Analyze the full execution history of a BlazeMeter test and produce a QA performance engineering assessment: trends in response time, throughput, error rates, anomaly patterns, and regression signals across runs.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

This is the canonical Context Resolution step from `shared/conventions.md` §4. Always resolve and **display** the full context (with ids) before doing any analysis, so the user can confirm you're operating on the right thing. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Identify the target test (two entry paths)

- **A `test_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a test *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

### Step 0b — Resolve the full hierarchy upward and confirm

Regardless of how the test was identified, always resolve and display its full organizational context before proceeding. Chain these calls — each response provides the ID needed for the next:

```
1. blazemeter_tests read         { test_id: <id> }
   → captures: test name, project_id

2. blazemeter_project read       { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read    { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read       { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into analysis.

Present the resolved context to the user before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a project_id is missing from the test response), **stop and report the gap** — do not proceed with analysis against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Collect all executions

```
blazemeter_execution list  { test_id: <id>, limit: 50, offset: 0 }
```

Page with `offset` in steps of 50 until all executions are retrieved. Capture for each execution:
- `execution_id`
- `start_time` / `end_time`
- `status` (only analyze `ENDED` / `PASSED` / `FAILED` — skip `TERMINATED`, `ERROR`)

Sort chronologically (oldest → newest) before analysis.

## Step 2 — Fetch reports for each execution

For each execution in scope, call **both** in parallel:

```
blazemeter_execution read_all_reports  { execution_id: <id> }
blazemeter_execution read_anomalies_stats  { execution_id: <id> }
```

`read_all_reports` returns three sub-reports:
- **summary** — aggregate KPIs: avg/p90/p95/p99 response time, throughput (RPS), error rate, concurrency, bandwidth
- **errors** — error breakdown by type, count, and percentage
- **request_stats** — per-endpoint (label) breakdown of the same KPIs

Also call `blazemeter_execution read` per execution to get its overall `execution_status` and `ended` timestamp. This response carries **only the overall pass/fail verdict** — not per-criterion results and not the criteria's `meta` labels. The failure-criteria **definitions** (`failure_criteria.rules[]`) and their readable `meta.*` labels come from the **test object** you already fetched in Step 0 (`blazemeter_tests read`). The MCP exposes no per-criterion per-run results array, so per-run criterion outcomes are inferred in Step 3f by comparing each run's summary KPIs against those thresholds.

## Step 3 — Analyze as a QA Performance Engineer

Work through these lenses in order:

### 3a. Trend analysis (time series across all runs)
Build a table: one row per execution, columns = date, avg RT, p90 RT, p95 RT, p99 RT, RPS, error rate %.

Flag any run where a KPI **moved ≥ 10%** from the prior run or from the rolling 5-run baseline — these are regression candidates.

### 3b. Response time distribution health
- Are p90/p95/p99 tracking proportionally? A widening gap (e.g. p99 climbing while p50 is flat) signals tail-latency problems — long-tail users being hit disproportionately.
- Identify the worst 3 runs and the best 3 runs.

### 3c. Error rate analysis
- Overall error rate trend (improving / stable / degrading).
- Top error types across all runs: which errors are consistent vs. one-off.
- Any runs where error rate spiked > 1% or > 5% — note these as incidents.
- Per-endpoint error breakdown from `request_stats` — pinpoint which labels are driving errors.

### 3d. Throughput & scalability
- RPS trend over time. Is the system handling the same load better or worse?
- Compare RPS vs. concurrent users — if load config changed between runs, normalize.

### 3e. Anomaly detection summary
From `read_anomalies_stats`:
- Count runs with `anomalies_with_details` vs `no_anomalies` vs `statistics_unavailable`.
- List recurring anomaly KPIs or time windows — a KPI that fires anomalies in 3+ runs is a systemic signal, not noise.

### 3f. SLA / failure criteria compliance
The overall pass/fail per run is its `execution_status` (from `blazemeter_execution read`); the criteria **definitions** + readable labels come from the test object (Step 0's `blazemeter_tests read` → `failure_criteria.rules[]` + `failure_criteria.meta.*`). The MCP returns no per-criterion per-run results, so attribute *which* criterion drove a failure by comparing each run's summary KPIs against the test's thresholds (e.g. an `error rate % > 4` rule against a run whose `error_rate_percent` is 26.67% → violated).
- How many runs PASSED vs FAILED overall (from `execution_status`)?
- Which specific criteria were likely driven past threshold (KPI-vs-threshold inference), and how often?

### 3g. Per-endpoint hot spots
From `request_stats` across runs:
- Rank endpoints by average p95 response time.
- Rank endpoints by error contribution.
- Flag any endpoint that degraded significantly in recent runs.

## Step 4 — Deliver the report

Structure the output as:

```
## BlazeMeter Test Analysis: <test name> (ID: <id>)
**Runs analyzed:** N  |  **Date range:** YYYY-MM-DD – YYYY-MM-DD

### Executive Summary
2–3 sentences: overall health trend, most pressing signal, recommendation.

### Trend Table
| Run | Date | Avg RT | p90 | p95 | p99 | RPS | Error % | Status |
...

### Key Findings
1. <Regression / improvement / pattern — with specific numbers>
2. ...

### Anomalies
- Runs with anomalies: N/total
- Recurring signals: ...

### Endpoint Hot Spots
| Endpoint | Avg p95 | Error % | Trend |
...

### SLA Compliance
- Pass rate: N/N runs
- Failing criteria: ...

### Recommendations
Prioritized, actionable items a developer/SRE could act on this sprint.
```

## Gotchas

- **Pagination**: the `list` action maxes at 50 per call — always check if total > 50 and page.
- **Skipping non-ENDED runs**: `TERMINATED` runs have incomplete data; including them distorts averages.
- **Load config changes**: if concurrency or duration changed between runs, note this — apples-to-apples comparison requires normalized throughput (RPS per virtual user).
- **`statistics_unavailable` anomalies**: this means the run was too short for the anomaly engine to build a baseline — not a signal, just insufficient data.
- **Failure criteria live on the test, not the execution**: `blazemeter_execution read` returns only the overall `execution_status`. The criteria definitions and their readable labels (`meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels`) come from the **test object** (Step 0's `blazemeter_tests read`) — render with those labels, never raw kpi ids or op codes, and infer per-run criterion outcomes from summary KPIs vs the test's thresholds.
- **Parallel fetches**: `read_all_reports` and `read_anomalies_stats` are independent per execution — call them in parallel to keep wall-clock time reasonable when analyzing many runs.
