---
name: bzm-compare-runs
description: Compare two BlazeMeter executions (baseline vs candidate) — diff response-time percentiles, throughput, and error rate with magnitude and direction, flag regressions past a threshold, and emit a ship / no-ship verdict. Use when asked to compare two runs, check a candidate against a baseline, gate a release on a load test, or decide whether a run regressed.
---

Compare two BlazeMeter executions — a **baseline** and a **candidate** — and produce a release-gate assessment: a KPI diff (response-time avg/p95/p99, throughput RPS, error rate) with magnitude and direction, regressions flagged past a 10% adverse-move threshold, per-endpoint deltas, normalization for load-config differences, ending in a ship / no-ship verdict with reasons.

**Division of labor (important):** the MCP is used for the *control plane* — resolving the two executions and their context interactively, the AI-consent gate, and any after-verdict drill-in (e.g. spelling out which failure criterion fired). The *data pull and all the arithmetic* — both runs' summary KPIs, request stats, anomaly status, every delta, the normalization, the regression flags — is done in **one invocation** of the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py`, which fetches the BlazeMeter API directly and returns one compact compare JSON. You read only that JSON and write the ship/no-ship narrative.

## Step 0 — Resolve and confirm context for BOTH executions (account → workspace → project → test → execution)

Context resolution here is **applied twice** — once per execution. Always resolve and **display** the full context (with ids) for **both** the baseline and the candidate before diffing anything, so the user can confirm you're comparing the right two runs. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. A comparison against the wrong run is worse than no comparison.

### Step 0a — Identify the two target executions (two entry paths)

You need exactly two `execution_id`s: a **baseline** and a **candidate**. Establish which is which up front — the diff direction (candidate relative to baseline) depends on it. **They must be two different executions** — the engine refuses to compare a run to itself. Resolve each target independently:

- **An `execution_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **A `test_id` (or test *name*) was given instead, or nothing** → resolve the test top-down first (account → workspace → project → test), applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test/execution (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - Then list that test's executions with `blazemeter_execution list { test_id, limit: 50, offset: 0 }` (page by 50) and let the user pick the baseline and the candidate. A common shape is **most-recent run as candidate, the prior passing run as baseline** — offer that as a confirmable suggestion, never a silent default.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

### Step 0b — Resolve each execution's full hierarchy upward and confirm

Do this **for each of the two executions**. Chain these calls — each response provides the ID needed for the next:

```
1. blazemeter_execution read   { execution_id: <id> }
   → captures: execution_status, ended (completion), created/updated, project_id, execution_name
   (Use execution_name as the run's display name. The execution API does NOT expose test_id,
    load config, or failure-criteria detail — so there is no blazemeter_tests read on this path.)

2. blazemeter_project read     { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read  { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read     { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**Completion gate:** an execution is only comparable once finished — in `blazemeter_execution read`, **`ended` must be NOT null** (`ended == null` ⇒ still running). If either run is still running, stop and say which one isn't done; comparing a partial run produces misleading KPIs.

**AI Consent gate:** AI access is gated **per account**. Check the consent state from step 4 for **each** execution's account. If an account has **not** enabled AI consent, stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into the comparison. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** the engine fetches anything.)

**Cross-account / cross-test comparison:** the two executions need not share an account, workspace, project, or even test. Resolving both hierarchies independently is what surfaces this. If they differ, that's allowed but **call it out explicitly** in the output (you may be comparing a run from a different scenario) and disambiguate same-named entities by their ids.

Present **both** resolved contexts to the user before continuing:

```
Baseline execution:  <execution_id>
  Test:       <execution_name>  (test id not exposed by the execution API)
  Project:    <project name>  (ID: <project_id>)
  Workspace:  <workspace name>  (ID: <workspace_id>)
  Account:    <account name>  (ID: <account_id>)

Candidate execution: <execution_id>
  Test:       <execution_name>  (test id not exposed by the execution API)
  Project:    <project name>  (ID: <project_id>)
  Workspace:  <workspace name>  (ID: <workspace_id>)
  Account:    <account name>  (ID: <account_id>)
```

If any link in either chain fails (e.g. a `read` returns 403, or `blazemeter_execution read` is missing `project_id`, or a parent `read` is missing `workspace_id` / `account_id`), **stop and report the gap** — do not diff against an unverified context. (Do **not** stop because `test_id` is missing — the execution API never returns it on this path, so that is expected, not a failure.) Once confirmed, carry the account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Run the comparison engine

One engine invocation fetches both runs' reports and does every piece of arithmetic — summary KPIs, per-endpoint request stats, anomaly status, all deltas, load normalization, and the regression flags:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py run-pair \
  --baseline-id <baseline_execution_id> \
  --candidate-id <candidate_execution_id> \
  --out <scratch>/compare.json
```

- The engine reads the **same credentials the MCP uses** from the environment — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file). Never pass keys on the command line. If it exits with a credentials error, show the user which variables to set and stop.
- Stdout is a **five-line summary** (statuses, KPI availability, adverse moves, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out` (a session scratch path, never the user's repo).
- `--no-anomalies` skips the anomaly-status fetches if the user doesn't want them.
- **Exit codes:** `0` success; `2` usage error — including the **same-id guard** (baseline and candidate are the same execution: go back to Step 0a and pick a real baseline) and missing credentials; `3` an execution itself couldn't be read (bad id or no access — report it, don't guess). Missing *sub-reports* never fail the run: they degrade into the JSON's `coverage` block and `notes`.

## Step 2 — Read the compare JSON

Read `--out`. Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. The shape:

- `baseline` / `candidate` — per run: `execution_id`, `name`, `test_id` (when the API exposes it), `report_status` (`pass | fail | unset | abort | error | noData`), `created` / `ended` (epoch seconds), `still_running`, `kpis` (avg/p90/p95/p99 ms, hits, throughput RPS, error-rate %, max_users, RPS-per-VU, duration — or `null` when the run has no load KPIs), `anomaly_status` (`no_anomalies | anomalies_with_details | statistics_unavailable`).
- `kpi_deltas` — candidate vs baseline per KPI (`avg`, `p95`, `p99`, `throughput`, `error_rate`), each with a `pct` and an `adverse` flag. **Adverse means a ≥10% move in the worse direction** (higher latency/error rate, lower throughput). Two built-in subtleties:
  - **Load normalization:** when the two runs' achieved peak concurrency (`max_users`) differs, throughput is judged on **RPS per virtual user** instead of raw RPS (flagged `normalized_per_vu: true`) so a smaller load isn't misread as a throughput regression.
  - **Error rate from a clean baseline:** when the baseline's error rate was 0%, a relative change is undefined — the delta carries `pct: null` and a `points` value (absolute percentage points), adverse when it crosses 1%.
- `endpoints` — per-label deltas for every label present **in both runs** (`matched`, sorted worst-first, each with baseline/candidate KPIs, `deltas`, and a `worst_kpi_move`), plus `baseline_only` / `candidate_only` label lists (endpoints that appeared or disappeared between the runs).
- `verdict_inputs` — the decision inputs: both `report_status` values, `candidate_failed_while_baseline_passed`, `adverse_kpi_moves` (names), `worst_kpi_move`, `regressed`, `load_config_differs`, `endpoints_with_adverse_moves`.
- `notes` — honesty flags: `baseline_kpis_unavailable` / `candidate_kpis_unavailable` (the summary had no load KPIs — e.g. GUI/EUX-style runs — or couldn't be fetched; **never presented as a clean 0%**), `*_request_stats_unavailable`, `*_still_running`.
- `coverage` — fetch bookkeeping (`http_attempted`, `http_failed`, named `failures`). Surface any failures honestly; never present a partial compare as complete.

## Step 3 — Verdict (ship / no-ship)

Decide from `verdict_inputs` and the notes — your contribution is judgment and prose, not arithmetic:

- **NO-SHIP** if any of: `candidate_failed_while_baseline_passed` is true; `error_rate` is adverse (past 10% relative, or crossed 1% in points from a clean baseline — and call out 5%+ as severe); `p95` or `p99` is adverse.
- **SHIP** if `regressed` is false (no adverse move anywhere), the candidate's `report_status` is `pass` (or `unset` with everything else clean — but say the pass/fail signal is indeterminate), and coverage is clean.
- **SHIP WITH CAVEATS / INCONCLUSIVE** if the only adverse moves are latency/error-rate shifts while `load_config_differs` is true (higher load legitimately changes them — only throughput is normalized; say what would make it conclusive, e.g. re-run the candidate at the baseline's concurrency), or KPIs/sub-reports are missing (`notes`, `coverage`), or either run's `report_status` is `abort` / `error` / `noData` / `unset`.

Weight the KPIs: error rate usually dominates, then p95/p99 tail latency, then throughput, then avg. Use `endpoints.matched` to name the offending endpoint(s) — a run-level regression concentrated in one label is a sharper finding than a diffuse one. Treat `anomaly_status: statistics_unavailable` as insufficient data, never as "no anomalies". Always give **reasons tied to specific numbers**, leading with the single most decision-relevant KPI.

## Step 4 — Deliver the report

```
## BlazeMeter Run Comparison
**Baseline:**  exec <baseline_id> — <execution_name> (<date>)
**Candidate:** exec <candidate_id> — <execution_name> (<date>)
**Threshold:** regression flagged at ≥ 10% adverse move (error rate from a 0% baseline: ≥ 1 point)

[CONFIG MISMATCH WARNING — only when load_config_differs]
Baseline achieved peak: <max_users> VU  |  Candidate achieved peak: <max_users> VU
→ Throughput judged per virtual user (normalized). Latency and error rate don't normalize
  across load levels — treat their diffs with lowered confidence.

### Verdict: SHIP / NO-SHIP / SHIP WITH CAVEATS
1–2 sentences leading with the decisive KPI, citing numbers.

### KPI Diff
| KPI | Baseline | Candidate | Δ% | Direction | Flag |
|-----|----------|-----------|----|-----------|------|
| Avg RT (ms)      |  |  |  | improved/regressed/flat | ✅ / ⚠️ REGRESSION |
| p95 RT (ms)      |  |  |  |  |  |
| p99 RT (ms)      |  |  |  |  |  |
| Throughput (RPS) |  |  |  |  |  |  ← per-VU when normalized (say so)
| Error rate (%)   |  |  |  |  |  |  ← points, not %, from a 0% baseline

### Endpoint deltas
- <label>: avg <b>→<c> ms (<Δ%>), errors <b>%→<c>%  ← worst matched endpoints first
- New in candidate: <candidate_only labels>   |   Gone from candidate: <baseline_only labels>
(or: "No matched endpoints to compare.")

### Regressions
- <KPI>: <baseline> → <candidate> (<Δ% or points>), past threshold. <one-line consequence>
(or: "None past threshold.")

### Reasons
- <bullet per factor that drove the verdict, tied to numbers>

### Notes / Caveats
- <config mismatch, KPIs unavailable, fetch coverage gaps, cross-account/cross-test, anomaly status, still-running>
```

## Step 5 — Drill-ins stay interactive

When the user asks a follow-up about one run ("*why* did the candidate fail?", "show me the errors"), that is a single-run question — answer it with the MCP (`blazemeter_execution read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure`). Don't re-run the engine for it. To spell out *which* failure criterion fired you need the criteria definitions, which live on the **test object** — ask the user for the test id and `blazemeter_tests read` it (the execution-entry path never exposes a `test_id`); never invent criteria.

## Gotchas

- **Trust the engine's arithmetic.** Deltas, direction, normalization, and the adverse flags are computed deterministically and fixture-tested — lower latency/error rate is better, higher throughput is better, and the flags already encode that. Your job is the verdict and the narrative; if a number looks wrong, say so and show it, don't silently recompute.
- **Don't compare a run to itself.** The engine exits with an error when baseline and candidate ids are equal. Offer the user a different baseline (e.g. the prior passing run) instead of retrying.
- **Apples-to-oranges load levels.** `load_config_differs` means the achieved peak concurrency differed. Throughput is already normalized per-VU; latency and error rate are **not** normalizable across load levels — report their raw diffs with a prominent CONFIG MISMATCH warning and lower verdict confidence rather than pretending they're comparable. Only *achieved* peak concurrency is visible here — the configured shape (hold-for/ramp-up/iterations) lives on the test object, so don't claim to compare it.
- **`kpis: null` is not a clean run.** It means the run produced no load KPIs (e.g. a GUI/EUX-style run whose summary row is all nulls) or the summary couldn't be fetched — an "unavailable", never a 0% error rate. With either side null there are no `kpi_deltas`; the verdict rests on `report_status` alone and should say so.
- **Completion before comparison.** Step 0's `ended != null` gate is the real check; the JSON's `still_running` flag is a backstop. A still-running execution returns partial KPIs that look like a regression.
- **Indeterminate failure status.** `report_status` can be `unset` (no criteria defined ⇒ no pass/fail signal), `abort`, `error`, or `noData` — none of these are a clean "pass". Don't read `unset` as "passed"; surface it as indeterminate in the verdict.
- **Tiny absolute values, huge percentages.** Error rate going 0.02% → 0.06% is +200% relatively but operationally trivial. Show absolute values alongside percentages and don't let a large Δ% on a negligible base dominate the verdict; conversely, flag any absolute crossing of 1% / 5% even when the relative move is small.
- **Endpoint sets can shift.** `baseline_only` / `candidate_only` labels mean the two runs didn't exercise the same endpoints — often a script change. Call it out; a "regression" on a barely-overlapping endpoint set is really a scenario change.
- **`statistics_unavailable` is not a finding.** Anomaly stats couldn't be read (run too short, or the endpoint unavailable) — insufficient data, never "anomalies detected" and never a clean bill.
- **Cross-account consent.** Each execution's account is consent-gated independently; both must have AI consent enabled, or Step 0 stops.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the JSON, or in the conversation. Scratch files (`compare.json`) go in the session scratch directory, not the user's repo.
- **Pagination.** When picking executions from a test, `blazemeter_execution list` maxes at 50 per call — page by `offset` if the run you want is older than the first page.
