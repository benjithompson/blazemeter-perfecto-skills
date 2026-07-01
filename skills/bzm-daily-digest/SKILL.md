---
name: bzm-daily-digest
description: Sweep every BlazeMeter test that ran across an account (or a chosen workspace/project) in a time window (default last 24h) and produce ONE cross-test scorecard — per-test pass/fail, regression vs each test's own baseline, ranked incidents, and a prioritized "what needs your eyes today" list. Use when asked for a daily/standup digest, a morning rollup, an overnight summary, or a "what broke since yesterday?" view across many tests at once.
---

Produce the **daily digest**: one cross-test scorecard for a whole account — or a chosen workspace/project — over a window. Where `bzm-analyze-test` trends *one* test deeply and `bzm-triage-failure` diagnoses *one* run, this skill sweeps **every test that ran** in the window, judges each against **its own baseline** (not just absolute pass/fail), and rolls the whole portfolio up into a scoreboard, a ranked incident list, and a short "needs your eyes today" list. It is markdown/terminal-first — a scannable standup artifact, **not** a branded HTML report (reach for `bzm-report` when you want the shareable HTML).

## Step 0 — Resolve the account, ask the user the rollup scope, then enumerate its tests

This is the **cross-test** variant of Context Resolution. A digest operates over **many tests at once**, so it resolves the **account**, then **asks the user how wide to roll up** — the **whole account** (every workspace → project → test), a **single workspace**, or a **single project** — and enumerates the tests in that chosen scope. It **never assumes** the breadth and never narrows to a single test. **Don't assume:** the user may belong to multiple accounts, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Resolve the account (tiered pick rule)

Resolve **only the account** here — which workspaces/projects you enumerate depends on the scope the user picks in Step 0b. Apply the uniform tiered pick rule to the account:

- Start from the `blazemeter_user read` default account, but **don't assume it's unambiguous — enumerate to see how many accounts exist**: exactly one → **display** it and proceed; more than one → present the pick and **stop** for the user's choice (never silently take the default).
- To enumerate, list one page (`blazemeter_account list`, `limit: 50`).
  - **Fits a choice list** (the first page is *not* full) → present an **interactive choice list**, every entry showing name + id (default marked), the user clicks one; if there are more accounts than the choice widget holds, fall back to a **numbered text list** with ids.
  - **Too big / paginated** (the first page comes back full → more pages exist) → **don't dump it**; ask the user to **name, paste an id, or filter**. A pasted **id short-circuits** via a direct `read`; a **name** you resolve by paging and matching.
- Always show the **id** next to each name. **Name doesn't resolve cleanly:** no match → say so, show what *is* available, stop; multiple matches → list each with its id and let the user pick; 403 → report the access gap, don't retry. **Never fall back to the default.**

### Step 0b — Ask the user the rollup scope (account / workspace / project)

Once the account is confirmed, **ask the user how wide to roll up — never assume**. Offer three altitudes as a choice list:

- **Whole account** — sweep **every workspace → project → test** in the account (the true "analysis of the day" across everything).
- **A single workspace** — roll up **all projects/tests** in one workspace. Resolve the workspace with the same tiered pick rule as Step 0a (choice list; **name / paste-id / filter** when the account has a large/paginated workspace list — e.g. >50).
- **A single project** — roll up one project's tests. Resolve workspace → project with the same tiered pick rule.

Offer **whole account** as the natural default for a "daily digest", but let the user choose — a workspace or project scope is equally valid. **Resolve only the levels the chosen scope needs** (account scope needs no workspace/project pick).

### Step 0c — AI Consent gate

Check the resolved **account's** AI-consent state via `blazemeter_account read`. If the account has **not** consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` — before enumerating or fetching anything.

### Step 0d — Enumerate the tests in the chosen scope

Enumerate to completion over whatever scope was picked in 0b — a full first page is **not** a reason to ask the user to name one test; keep paging:

- **Whole account** → for **each workspace** in the account (page `blazemeter_workspaces list`), list its projects (`blazemeter_project list`), then each project's tests (`blazemeter_tests list`, `offset` by 50). Parallelize across workspaces/projects.
- **Workspace** → each project in the workspace → its tests.
- **Project** → `blazemeter_tests list { project_id: <id> }` to completion.

Capture each test's `test_id`, name, **and its workspace/project** so an account-wide scoreboard can group by workspace.

**Practicality guard:** a whole-account sweep of a **large account** (the first `blazemeter_workspaces list` page comes back full → >50 workspaces, or the test count runs into the hundreds) is heavy — **say so up front**, show how many workspaces/tests are in play, and offer to **narrow to specific workspaces/projects** or to proceed with the full sweep. Never silently truncate to the first page.

### Step 0e — Display the resolved scope and the test count, then continue

Display the cross-test context block before acting, so the run is auditable:

```
Scope:      Whole account  (all workspaces)                   ← or "Workspace <name> (ID)" / "Project <name> (ID)"
Account:    <account name>  (ID: <account_id>)
Workspaces: <N swept>                                         ← for account scope; omit for a workspace/project digest
Window:     <resolved window, e.g. last 24h: 2026-06-26 09:00 → 2026-06-27 09:00>
Tests:      <N> tests in scope
```

Carry this resolved scope forward as **conversational memory** for later skills in the same conversation (display it, allow a one-step "switch"); **never persist it** to disk.

## Step 1 — Resolve the window

Default to the **last 24 hours** ending now. Let the user override in natural language — "since yesterday", "last 3 days", "this week", or an explicit date range ("2026-06-20 to 2026-06-26"). Compute a concrete `[from, to]` timestamp pair and **display it** (in Step 0e's block) so the user can see exactly what counts as "today". Everything downstream filters runs by `start_time`/`end_time` falling inside this window.

## Step 2 — For each test, list the runs that fall in the window

For every test enumerated in Step 0d, list its executions and keep only those that **overlap the window**:

```
blazemeter_execution list  { test_id: <id>, limit: 50, offset: 0 }
```

- **Pagination:** the `list` action maxes at 50 per call. Executions come back newest-first; page by `offset` only until you pass the start of the window (once a page's runs are all older than `from`, you can stop paging that test).
- Keep runs whose `start_time`/`end_time` fall within `[from, to]`. Capture per run: `execution_id`, `start_time`/`end_time`, and `status` (`execution_status`).
- **Statuses to skip (like `analyze`):** only roll up `ENDED`/`PASSED`/`FAILED` runs. **Skip `TERMINATED` and `ERROR`** — they have incomplete data that would distort the scorecard. Count them separately as "skipped (partial)" so the digest is honest about what it didn't read, but don't fold their KPIs in.
- A test with **no runs in the window** is simply absent from the scoreboard's active rows (note the count of idle tests in the summary — see Step 5).

These per-test list calls are **independent — parallelize them** across tests to keep wall-clock time reasonable on a large scope.

## Step 3 — For each in-window run, fetch its KPIs and anomalies

For each in-scope run (an `ENDED`/`PASSED`/`FAILED` execution inside the window), fetch in **parallel** — both per run, and across runs/tests, since every fetch is independent:

```
blazemeter_execution read_all_reports     { execution_id: <id> }   # summary / errors / request_stats
blazemeter_execution read_anomalies_stats { execution_id: <id> }   # anomaly detection status
```

`read_all_reports` returns three sub-reports (same shape `analyze` uses):
- **summary** — aggregate KPIs: avg / p90 / p95 / p99 response time, throughput (RPS), error rate %, achieved peak concurrency, bandwidth.
- **errors** — error breakdown by type, count, and %.
- **request_stats** — per-endpoint (label) KPIs.

`read_anomalies_stats` returns `anomaly_detection_status` ∈ `no_anomalies | anomalies_with_details | statistics_unavailable`, with per-anomaly detail (KPI + window) when present.

> **Parallelism note (like `analyze`):** these are independent per execution **and** across executions/tests — fan them out in parallel rather than serially, or a multi-test window can take a long time.

## Step 4 — Judge each test against ITS OWN baseline (the crucial step)

Absolute pass/fail is not enough: a run can pass its criteria yet be **meaningfully slower than the test's golden baseline**, and that newly-regressed-but-still-green case is exactly what a daily digest exists to surface. So for **each test** that ran in the window, resolve **that test's own baseline** and compare its in-window run(s) against it.

### 4a. Resolve the per-test baseline (pinned → CI file → last-passing)

Reuse the baseline concept and the **shared script** from `bzm-baseline` — **do not** re-implement baseline logic. Resolution order, per test:

1. **Conversational pin** — if the user pinned a baseline `execution_id` for this test earlier in the conversation, use it.
2. **Committed CI file** — if the repo has a `.blazemeter/baseline.json`, read its entry for this `test_id`:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py resolve \
     --file .blazemeter/baseline.json --test-id <test_id>
   ```

   It prints `{"source": "pinned", "execution_id": "<id>"}` when the file has an entry. A **malformed** file exits non-zero — report that for that test and fall through to last-passing only if the user is fine with it; don't silently swallow it.
3. **Last-passing run** — with no pin and no file entry, default to the test's most recent passing run. Build a JSON list of the test's executions (from Step 2, paging further back than the window if the window itself contains no pass — the baseline can predate the window) with `id`, `status`, `end_time`, and let the script choose:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py last-passing \
     --executions <executions.json>
   ```

   If it returns `null`, this test has **no passing run to baseline against** — mark its regression column **"no baseline"** rather than inventing one, and lean on its absolute pass/fail instead.

Read the resolved baseline's KPIs once per test with `blazemeter_execution read_all_reports { execution_id: <baseline_id> }` (the **summary** sub-report) — that's the reference the window's runs are measured against. Don't compare a run to itself: if the only in-window run *is* the resolved last-passing baseline, there's nothing to regress against yet — note it as "baseline run, no prior to compare".

### 4b. Compute the regression verdict per test

For each test, compare its **most significant in-window run** (the newest, or the worst if several ran) against its baseline summary KPIs, using the same **≥ 10% KPI move** heuristic `analyze` uses:

- Compute the % change for avg RT, p95 RT, p99 RT, throughput (RPS, inverted — *lower* is worse), and error rate.
- A test is **newly-regressed** if any tracked KPI moved **≥ 10%** in the worse direction vs its baseline. Record the **worst KPI move** (the single largest adverse % change, named — e.g. `p95 +34%`) for the scoreboard.
- **Normalize for load-config changes:** if a run's concurrency/duration differs from the baseline, raw RPS isn't comparable — normalize throughput to **RPS per virtual user** before judging it, and note the normalization (don't flag a regression that is really just a smaller load).
- A run that **fails its criteria** is a regression regardless of KPI deltas — failure always lands on the scoreboard.

## Step 5 — Roll the portfolio up

Synthesize Steps 2–4 into the three rollups:

### 5a. Scoreboard
One row per test that ran in the window: `# runs`, overall `pass/fail` (e.g. `3/4 passed`), **newly-regressed?** (vs the test's own baseline), and **worst KPI move** (named). Sort the worst offenders to the top (failures first, then largest regressions).

### 5b. Top incidents — ranked by severity
Pull the notable signals across **all** tests into one ranked list. Rank by severity using this order (it mirrors `triage`'s systemic-vs-noise framing applied portfolio-wide):

1. **Outright failures** — a run that failed its criteria (or `abort`/`error` status within the data you kept). Highest severity.
2. **Large regressions vs baseline** — a still-green run that moved a KPI well past 10% (the bigger the move and the more tests affected, the higher).
3. **Error-rate spikes** — a run whose overall error rate crossed a meaningful bar (e.g. > 1%, and especially > 5%), or an endpoint at a near-100% error rate even at low traffic.
4. **Anomalies** — `anomalies_with_details` runs; weight a KPI/window that recurs across **multiple tests or runs** as **systemic** (higher) over a lone one-off (lower / likely noise).

For each incident name the **test, run, the metric, and baseline-vs-now numbers** so it's actionable. Treat `statistics_unavailable` as **insufficient data, not a finding** (never an incident); call out `noData`/`unset` runs as inconclusive rather than as clean.

### 5c. What needs your eyes today
A short (3–7 item) **prioritized** list distilled from the incidents — the single highest-leverage things a person should look at this morning, each one line, ordered by severity, each pointing at the test/run and why. This is the "if you read nothing else" section.

## Step 6 — Handle the edge cases gracefully

- **Empty window (nothing ran):** if **no** test in scope had an in-window `ENDED`/`PASSED`/`FAILED` run, **do not** fabricate a scoreboard. Emit the short empty-window form: confirm the scope and window, state plainly that **nothing ran in this window**, note how many tests are in scope (and, if useful, when each last ran), and stop. An empty digest is a valid, useful answer — say it clearly.
- **Partial-data / non-ENDED runs:** `TERMINATED`/`ERROR` runs are skipped from KPIs (Step 2) but **counted** as "skipped (partial)" in the summary so the digest is honest about coverage.
- **No baseline for a test:** mark its regression column "no baseline" and fall back to absolute pass/fail — don't guess.

## Output template

```
## BlazeMeter Daily Digest — <scope name> (Account/Workspace/Project ID: <id>)
**Window:** <from> → <to>   |   **Tests in scope:** N   |   **Ran in window:** M   |   **Account:** <account name> (<account_id>)

### TL;DR — what needs your eyes today
1. <highest-severity item — test/run + one-line why>
2. ...
(3–7 prioritized items; the "if you read nothing else" list)

### Scoreboard
| Test | Runs | Pass/Fail | Newly regressed? | Worst KPI move | Baseline source |
|------|------|-----------|------------------|----------------|-----------------|
| <test name> (id) | 4 | 3/4 | ⚠ yes | p95 +34% vs baseline | last-passing |
| ...              |   |     | ok    | —                    | committed file  |
(failures first, then largest regressions; idle tests omitted here — see footer)

### Top incidents (ranked by severity)
1. **[FAIL]** <test> run <exec_id> — failed criteria; error rate 26.7% (baseline 0.4%).
2. **[REGRESSION]** <test> run <exec_id> — p95 480ms → 642ms (+34%) vs baseline <baseline_id>; still green.
3. **[ERROR SPIKE]** <test> run <exec_id> — /checkout 98% errors on 120 samples.
4. **[ANOMALY · systemic]** <KPI/window> recurred across <N> tests.
...
(skip statistics_unavailable as a finding; mark noData/unset runs inconclusive)

### Coverage notes
- Idle tests (no run in window): <N> (<names or "list on request">)
- Skipped (partial/non-ENDED) runs: <N> — TERMINATED/ERROR, KPIs not read
- Tests with no baseline: <N> — judged on absolute pass/fail only
- Normalized for load-config change: <tests, if any>
```

For an **empty window**, collapse the body to:

```
## BlazeMeter Daily Digest — <scope name> (<id>)
**Window:** <from> → <to>   |   **Tests in scope:** N

Nothing ran in this window. No executions to report.
(Most recent run across scope: <test> at <timestamp>, if useful.)
```

## Gotchas

- **Ask the scope; then enumerate, don't narrow to one test.** Step 0 resolves the **account**, **asks** the user the rollup breadth (whole account / a workspace / a project — never assumed), and enumerates every test in it — a full first page of `blazemeter_tests list` means "keep paging", **not** "ask the user to name one test". A whole-account sweep of a large account (>50 workspaces / hundreds of tests) is the one case to flag up front and offer to narrow.
- **Pagination.** Every `list` action (`workspaces`, `project`, `tests`, `execution`) maxes at **50 per page** — page by `offset`. For executions, stop paging a test once a page is entirely older than the window's `from`.
- **Statuses to skip.** Roll up only `ENDED`/`PASSED`/`FAILED`. **Skip `TERMINATED`/`ERROR`** (incomplete data) — count them as "skipped (partial)", never fold their KPIs into the scoreboard.
- **Load-config normalization.** If concurrency/duration changed between a run and its baseline, raw RPS isn't comparable — normalize to RPS-per-virtual-user before flagging a throughput regression, and say you did.
- **`statistics_unavailable` is not a finding.** It means the run was too short for the anomaly engine to build a baseline — insufficient data, never reported as "anomalies detected" or as a clean run. Likewise `noData`/`unset` runs are inconclusive, not green.
- **Per-test baseline caveats.** The baseline is resolved **per test** (pinned → committed `.blazemeter/baseline.json` → last-passing) via the shared `bzm_baseline.py` — don't re-implement that logic, and don't share one baseline across tests. If `last-passing` returns `null`, the test has **no baseline**: mark it "no baseline" and fall back to absolute pass/fail rather than inventing a reference. A baseline may legitimately predate the window, so page execution history further back than the window when the window itself contains no passing run. A malformed committed file exits non-zero — surface it, don't silently swallow it.
- **Don't compare a run to itself.** If a test's only in-window run *is* its resolved last-passing baseline, there's no prior to regress against — note "baseline run" rather than reporting a 0% (or spurious) move.
- **Failure criteria live on the test, not the execution.** `blazemeter_execution read` returns only the overall `execution_status`; the per-criterion thresholds and readable labels live on the **test object** (`blazemeter_tests read` → `failure_criteria.rules[]` + `meta.*`). At digest altitude, lead with the overall verdict and the KPI-vs-baseline deltas; reach into the test object's criteria only when explaining *why* a specific run failed.
- **MCP-first.** Every retrieval here is a `blazemeter_*` MCP action (`*_list`, `*_read`, `read_all_reports`, `read_anomalies_stats`); no REST v4 fallback is needed. Only a genuine MCP gap would justify a documented REST call.
- **Parallelize.** Per-test execution lists, and per-execution `read_all_reports` + `read_anomalies_stats`, are all independent — fan them out in parallel; a serial sweep over a whole workspace is needlessly slow.
- **Never persist scope.** The resolved account/workspace/project is conversational memory only — carried forward within the conversation, never written to disk. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing.
