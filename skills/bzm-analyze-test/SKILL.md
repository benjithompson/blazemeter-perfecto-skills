---
name: bzm-analyze-test
description: Analyze BlazeMeter test execution results over time as a QA Performance engineer. Use when asked to analyze, review, trend, or report on a BlazeMeter test's performance history, regressions, error patterns, or SLA compliance.
---

Analyze the execution history of **one** BlazeMeter test over a window and produce a QA performance engineering assessment: trends in response time, throughput, error rates, anomaly patterns, and regression signals across runs.

**Division of labor (important):** the MCP is used for the *control plane* — resolving the test interactively, the AI-consent gate, and any after-analysis drill-in on a single run. The *bulk data pull* (listing every execution, fetching every run's reports) is **never** done by chaining MCP calls — a busy test's history is dozens of runs and hundreds of payloads. It is handed off to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py`, whose `history` subcommand pulls the test's runs from the BlazeMeter API directly, does all the arithmetic (window filtering, status bucketing, per-run KPIs, baseline resolution, per-run deltas, normalization), and returns one compact pre-aggregated JSON. You read only that JSON and write the trend narrative and severity judgment.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

Always resolve and **display** the full context (with ids) before doing any analysis, so the user can confirm you're operating on the right thing. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

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
   → captures: test name, project_id, failure criteria (used in Step 3)

2. blazemeter_project read       { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read    { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read       { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into analysis. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

Present the resolved context to the user before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a project_id is missing from the test response), **stop and report the gap** — do not proceed with analysis against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Resolve the window

Default to the **last 30 days** ending now — enough runs for a trend without pulling years of history. Let the user override in natural language — "last quarter", "since the 2.3 release", "all of June", or an explicit date range. Compute a concrete `[from, to]` timestamp pair and **display it** alongside the context block so the user can see exactly which runs are in scope. If the window turns out to contain no runs (Step 3), offer to widen it rather than fabricating an analysis.

## Step 2 — Pull the run history with the engine

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
- Stdout is a **five-line summary** (runs in window, pass/fail, baseline, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out`.
- **Exit codes:** `0` success; `2` usage/credentials (tell the user what to set/fix); `3` scope-level failure (e.g. a bad test id) **or** too many fetch failures (default threshold 20%, tune with `--max-failure-rate`). On `3` the JSON may still exist — its `coverage` block says exactly what's missing; report the analysis as **partial**, never as complete.

## Step 3 — Read the history JSON and analyze as a QA Performance Engineer

Read `--out`. It is compact — one entry per run in the window, **oldest first** (the trend axis). Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. You get:

- top-level counts: `runs_in_window`, `kpi_runs` (runs with a complete pass/fail verdict), `passed` / `failed`, `skipped_partial` (aborted/errored runs whose KPIs were deliberately not folded in), `inconclusive`, `still_running`;
- `baseline` (`source`: `pin | file | last-passing | none`, and the execution id), `baseline_kpis`, `candidate_execution_id` (the newest failing run if any failed, else the newest run), `regressed_runs`;
- per run in `runs[]`: `execution_id`, `started`/`ended`, `report_status`, `bucket`, `kpis` (avg/p90/p95/p99 ms, hits, throughput RPS, error-rate %, max users, RPS-per-VU, duration), `deltas` vs the baseline (avg/p95/p99/throughput/error-rate, each with a `pct` and an `adverse` flag — adverse means a ≥10% move in the worse direction; throughput is judged per-virtual-user when the load config changed, flagged `normalized_per_vu`), `worst_kpi_move`, `regressed`, `is_baseline`, `anomaly_status`, and `anomalies` (KPI + label) when present;
- `incident_candidates` (`failure`, `regression`, `error_spike`, `endpoint_error_spike`) and `notes` (`no_baseline`, `baseline_is_only_run`, `baseline_kpis_unavailable`; per-run `kpis_unavailable` also covers run types with no load KPIs, e.g. GUI/EUX runs).

Your contribution is **judgment and prose** — work through these lenses in order:

### 3a. Trend narrative
Walk `runs[]` chronologically. Is the test improving, stable, or degrading? Point at the specific runs where a KPI moved — a one-run blip reads differently from a three-run slide. Use each run's `deltas` and `worst_kpi_move`; a run flagged `regressed` moved a KPI ≥10% in the worse direction vs the baseline.

### 3b. Response-time distribution health
From each run's `kpis`: are p90/p95/p99 tracking proportionally? A widening gap (p99 climbing while avg is flat) signals tail-latency problems — long-tail users being hit disproportionately. Name the worst and best runs.

### 3c. Error-rate pattern
Error-rate trend across runs (improving / stable / degrading), and any run past 1% (incident) or 5% (severe — both already flagged as `error_spike` incidents). An `endpoint_error_spike` incident names a label erroring at near-100% on the newest problem run — the first place to point a developer.

### 3d. Throughput & scalability
RPS trend over time — is the system handling the load better or worse? When `max_users` differs between runs, compare `rps_per_vu` instead of raw RPS (the engine already does this for the baseline deltas, flagged `normalized_per_vu`) and say so.

### 3e. Anomaly recurrence
Count runs by `anomaly_status`. A KPI/label pair recurring in the `anomalies` of **3+ runs** is a systemic signal, not noise; a lone one-off is likely noise. Treat `statistics_unavailable` as **insufficient data, not a finding** — never an anomaly, never a clean bill.

### 3f. SLA / failure-criteria compliance
Per-run pass/fail is `report_status`. The criteria **definitions** and their readable labels come from the **test object** you already fetched in Step 0 (`blazemeter_tests read` — render the readable labels, never raw KPI ids or op codes). No per-criterion per-run results exist, so attribute *which* criterion drove a failure by comparing the failing run's `kpis` against the test's thresholds (e.g. an "error rate % > 4" rule against a run at 26.7% → violated). Report the pass rate and the criteria most often implicated.

## Step 4 — Deliver the report

```
## BlazeMeter Test Analysis: <test name> (ID: <id>)
**Window:** <from> → <to>  |  **Runs:** N in window (K with KPI verdicts)  |  **Baseline:** <source> <exec_id>

### Executive Summary
2–3 sentences: overall health trend, most pressing signal, recommendation.

### Trend Table
| Run | Date | Status | Avg RT | p90 | p95 | p99 | RPS | Error % | vs baseline |
...
(oldest → newest, KPI runs; mark the baseline row; "—" for runs with no KPIs)

### Key Findings
1. <Regression / improvement / pattern — with specific numbers and run ids>
2. ...

### Anomalies
- Runs with anomalies: N / <kpi runs>  (statistics unavailable: N)
- Recurring signals: <KPI @ label seen in M runs> ...

### SLA Compliance
- Pass rate: N/K runs
- Criteria implicated: <readable label> — likely violated in runs <ids> (KPI vs threshold)

### Recommendations
Prioritized, actionable items a developer/SRE could act on this sprint.

### Coverage notes                                  ← only when something is missing
- Skipped (aborted/errored) runs: N — KPIs not folded in
- Fetch coverage: <ok>/<attempted> (<failed> failed) — analysis is partial
```

For an **empty window** (`runs_in_window: 0`), don't fabricate a report: confirm the test and window, state plainly that nothing ran, and offer to widen the window.

## Step 5 — Drill-ins stay interactive

When the user asks about one run ("what happened in run 82525951?", "which endpoints were slow?"), that is a *single-run* question — answer it with the MCP (`blazemeter_execution read`, `read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure` for a failed run). Don't re-run the history pull for it, and don't page MCP reports across many runs — that's what Step 2 was for.

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_execution list` + per-run report reads burns enormous time and tokens on a busy test and is exactly what the engine exists for. MCP is for Step 0's interactive picks, the consent gate, and single-run drill-ins afterward — nothing in between.
- **Consent before pull.** The AI-consent check (Step 0b) must pass before any `history` invocation — the gate lives in the MCP layer, and the engine assumes it already happened.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the report, or in the conversation.
- **Trust the engine's arithmetic.** Deltas, normalization, baseline choice, and status buckets are computed deterministically and fixture-tested. Your job is trend narrative and severity — if a number looks wrong, say so and show it; don't silently recompute.
- **The engine already excludes partial runs.** Aborted/errored runs are counted (`skipped_partial`) but their KPIs never fold into the trend — including them would distort averages. Report the count.
- **`kpis_unavailable` is not a zero.** Some run types (e.g. GUI/EUX) report no load KPIs at all — such a run shows in the trend table as "—", never as a 0 ms / 0% row.
- **`statistics_unavailable` is not a finding.** It means anomaly stats couldn't be read (run too short for the anomaly engine to build its own baseline, or the stats endpoint unavailable) — insufficient data, never "anomalies detected" and never a clean bill.
- **Don't compare a run to itself.** A green run is never its own baseline — last-passing resolution excludes the candidate, so a still-green regression is detectable whenever any prior pass exists. `baseline_is_only_run` in the notes means a *pinned* baseline (conversational or committed file) points at the newest run itself — report "baseline run, no prior to compare", not a 0% move. The baseline run's own row carries `is_baseline` and no deltas.
- **Load-config changes break raw comparisons.** If `max_users` or duration changed between runs, note it — apples-to-apples throughput comparison uses RPS per virtual user (`rps_per_vu`), which the engine's deltas already switch to (`normalized_per_vu`).
- **Failure criteria live on the test, not the execution.** Per-run responses carry only the overall verdict; definitions and readable labels come from `blazemeter_tests read` in Step 0. Infer per-run criterion outcomes from the run's KPIs vs the thresholds, and say it's an inference.
- **Never persist scope.** The resolved account/workspace/project/test is conversational memory only. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing. Scratch files (`pins.json`, `history.json`) go in the session scratch directory, not the user's repo.
