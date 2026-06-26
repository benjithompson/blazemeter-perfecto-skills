---
name: compare-blazemeter-runs
description: Compare two BlazeMeter executions (baseline vs candidate) — diff response-time percentiles, throughput, and error rate with magnitude and direction, flag regressions past a threshold, and emit a ship / no-ship verdict. Use when asked to compare two runs, check a candidate against a baseline, gate a release on a load test, or decide whether a run regressed.
---

Compare two BlazeMeter executions — a **baseline** and a **candidate** — and produce a release-gate assessment: a KPI diff (response-time avg/p90/p95/p99, throughput RPS, error rate) with magnitude and direction, regressions flagged past a stated threshold, normalization for load-config differences where possible (and a clear warning when it isn't apples-to-apples), ending in a ship / no-ship verdict with reasons.

## Step 0 — Resolve and confirm context for BOTH executions (account → workspace → project → test → execution)

This is the canonical Context Resolution step from `shared/conventions.md` §4, **applied twice** — once per execution. Always resolve and **display** the full context (with ids) for **both** the baseline and the candidate before diffing anything, so the user can confirm you're comparing the right two runs. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. A comparison against the wrong run is worse than no comparison.

### Step 0a — Identify the two target executions (two entry paths)

You need exactly two `execution_id`s: a **baseline** and a **candidate**. Establish which is which up front — the diff direction (candidate relative to baseline) depends on it. Resolve each target independently:

- **An `execution_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **A `test_id` (or test *name*) was given instead, or nothing** → resolve the test top-down first (account → workspace → project → test), applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, presented as a confirmable/overridable suggestion; if a level has exactly one option, just display it.
  - To enumerate, list one page (`limit: 50`). **Small set** (page not full) → numbered list, each entry with its id, user picks. **Too big to list** (page comes back full) → don't dump it; ask the user to name or paste the workspace/project/test/execution (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
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

**AI Consent gate:** AI access is gated **per account**. Check the consent state from step 4 for **each** execution's account. If an account has **not** enabled AI consent, stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding into the comparison.

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

## Step 1 — Pull the reports for each execution

For each of the two executions, call in parallel (the two executions are independent of each other, so all four calls can overlap):

```
blazemeter_execution read_all_reports  { execution_id: <baseline_id> }
blazemeter_execution read_all_reports  { execution_id: <candidate_id> }
```

`read_all_reports` returns three sub-reports per execution:
- **summary** — aggregate KPIs: avg / p90 / p95 / p99 response time, throughput (RPS), error rate, concurrency, bandwidth
- **errors** — error breakdown by type, count, and percentage
- **request_stats** — per-endpoint (label) breakdown of the same KPIs

You already have each execution's `execution_status` and `ended` from the Step 0b `blazemeter_execution read`. Note the execution API does **not** return the configured load (concurrency/hold-for/ramp-up/iterations) or failure-criteria detail — those live on the **test object**, which this execution-entry path never resolves (no `test_id` is exposed). The only load signal available here is the **achieved peak concurrency**, `max_concurrent_users`, from the summary report (Step 1). Failure-criteria meta labels (`meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels`) would require a `blazemeter_tests read` with a `test_id` you don't have; if a verdict truly needs them, say so and ask the user for the test id rather than inventing one.

## Step 2 — Establish whether it's apples-to-apples (load normalization)

Before diffing, compare the **achieved peak concurrency** of the two runs — `read_summary.max_concurrent_users` from the summary report you already pulled in Step 1 (one per execution). This is the only load signal available on the execution-entry path: the *configured* load (hold-for / ramp-up / iterations) lives on the test object and is **not** reachable from executions alone, so don't claim to compare it — only achieved peak concurrency is comparable here. (Comparing the configured shape would require a `blazemeter_tests read` per run, which needs a `test_id` this path doesn't expose.)

- **Achieved concurrency matches** → a direct KPI diff is valid. Proceed to Step 3 on raw numbers.
- **Achieved concurrency differs** → raw KPIs are not directly comparable (more virtual users naturally lowers per-user throughput and inflates latency). **Normalize where you can** and **warn clearly**:
  - **Throughput**: compare **RPS per virtual user** = `average_throughput_per_second / max_concurrent_users` for each run, alongside raw RPS. This removes the load-level difference from the throughput signal.
  - **Response time / error rate**: these do **not** normalize cleanly across different concurrency — higher load legitimately changes them. Report the raw diff but mark it **not apples-to-apples** and lower your confidence in any verdict that hinges on it.
  - If achieved peak concurrency differs by more than a small margin (say > 10%), put a prominent **CONFIG MISMATCH** warning at the top of the output; do not let a regression flag masquerade as a code regression when it's really a load-level difference. (You can only attribute the gap to *achieved* load, not to a configured-load change, since the configured shape isn't visible here.)

## Step 3 — Diff the key KPIs (magnitude and direction)

For each KPI, compute the candidate's change relative to the baseline:

```
delta      = candidate - baseline
delta_pct  = (candidate - baseline) / baseline * 100   (guard baseline == 0)
```

KPIs to diff (use the normalized value where Step 2 produced one):

| KPI | Better when | Notes |
|-----|-------------|-------|
| Avg response time | lower | ms |
| p90 / p95 / p99 response time | lower | tail latency; weight p95/p99 heavily |
| Throughput (RPS) | higher | use raw **and** RPS-per-VU when configs differ |
| Error rate % | lower | a jump here usually dominates the verdict |

Record **direction** (improved / regressed / flat) per KPI, not just magnitude — a 15% *drop* in p95 is an improvement, a 15% *rise* is a regression. "Flat" = within the no-op band (e.g. |delta_pct| < 2%) to avoid treating noise as signal.

### Regression threshold

Flag a KPI as a **regression** when it moves in the worse direction by **≥ 10%** (state the threshold you used; let the user override it). Apply per KPI:
- response-time percentiles: regression = candidate higher by ≥ threshold
- throughput: regression = candidate lower by ≥ threshold
- error rate: regression = candidate higher by ≥ threshold (and call out any absolute crossing of 1% / 5% regardless of relative change — a jump from 0.1% to 0.9% is "only" under threshold relatively but still worth a note; a jump past 1% or 5% is an incident)

## Step 4 — Verdict (ship / no-ship)

Decide from the flagged regressions and the config-comparability check:

- **NO-SHIP** if any of: error rate regressed past threshold or crossed an absolute incident line; p95 or p99 regressed past threshold; the candidate FAILED its failure criteria while the baseline passed.
- **SHIP** if no KPI regressed past threshold (improvements or flat across the board), configs are comparable (or normalized cleanly), and failure criteria held.
- **SHIP WITH CAVEATS / INCONCLUSIVE** if the only regressions are explained by a **config mismatch** (Step 2), or data is missing/partial — say what would make it conclusive (e.g. re-run the candidate at the baseline's concurrency).

Always give **reasons** tied to specific numbers, and lead with the single most decision-relevant KPI.

## Step 5 — Deliver the report

Structure the output as:

```
## BlazeMeter Run Comparison
**Baseline:**  exec <baseline_id> — <execution_name> (<date>)
**Candidate:** exec <candidate_id> — <execution_name> (<date>)
**Threshold:** regression flagged at ≥ <N>%

[CONFIG MISMATCH WARNING — only if Step 2 found one]
Baseline achieved peak: <max_concurrent_users> VU  |  Candidate achieved peak: <max_concurrent_users> VU
(Configured hold-for/ramp-up/iterations are not visible on the execution-entry path, so only achieved peak concurrency is compared.)
→ <which KPIs are / are not apples-to-apples, and what was normalized>

### Verdict: SHIP / NO-SHIP / SHIP WITH CAVEATS
1–2 sentences leading with the decisive KPI, citing numbers.

### KPI Diff
| KPI | Baseline | Candidate | Δ | Δ% | Direction | Flag |
|-----|----------|-----------|---|----|-----------|------|
| Avg RT (ms)        |   |   |   |   | improved/regressed/flat | ✅ / ⚠️ REGRESSION |
| p90 RT (ms)        |   |   |   |   |   |   |
| p95 RT (ms)        |   |   |   |   |   |   |
| p99 RT (ms)        |   |   |   |   |   |   |
| Throughput (RPS)   |   |   |   |   |   |   |
| RPS per VU         |   |   |   |   |   |   |  ← shown only when configs differ
| Error rate (%)     |   |   |   |   |   |   |

### Regressions
- <KPI>: <baseline> → <candidate> (<Δ%>), past the <N>% threshold. <one-line consequence>
(or: "None past threshold.")

### Reasons
- <bullet per factor that drove the verdict, tied to numbers>

### Notes / Caveats
- <config mismatch, missing data, cross-account/cross-test, anomalies, failure-criteria detail>
```

## Gotchas

- **Direction matters as much as magnitude.** Lower is better for latency and error rate; higher is better for throughput. A naive `|delta_pct| ≥ threshold` flag will mislabel improvements as regressions — always pair magnitude with the worse-direction check from Step 3.
- **Apples-to-oranges load levels.** Different achieved concurrency makes a raw KPI diff meaningless. Normalize throughput as **RPS per virtual user** (`average_throughput_per_second / max_concurrent_users`) and warn loudly; latency and error rate don't normalize across load levels, so lower verdict confidence rather than pretending they're comparable. Note only *achieved* peak concurrency (`max_concurrent_users`, from the summary) is available on the execution-entry path — the configured load (hold-for/ramp-up/iterations) lives on the test object and isn't reachable from executions alone, so don't claim to compare it.
- **Completion before comparison.** Use `ended != null` (not status text) to confirm each run finished. A still-running execution returns partial KPIs that look like a regression.
- **Indeterminate failure status.** `execution_status` can be `unset` (no criteria defined ⇒ no pass/fail signal), `abort`, `error`, or `noData` — none of these are a clean "pass." Don't read `unset` as "passed"; surface it as indeterminate in the verdict.
- **Baseline of zero.** Guard `delta_pct` when a baseline KPI is 0 (e.g. error rate). Report the absolute change and label it "n/a%" rather than dividing by zero or printing infinity.
- **Tiny absolute values, huge percentages.** Error rate going 0.02% → 0.06% is +200% relatively but operationally trivial. Show absolute values alongside percentages and don't let a large Δ% on a negligible base dominate the verdict; conversely, flag any absolute crossing of 1% / 5% even if the relative move is small.
- **Failure-criteria pass/fail vs. detail.** The pass/fail signal is the execution's own `execution_status` (`pass`/`fail`) — that's all you need for the "candidate failed while baseline passed" verdict, and it's available on this path. The criteria *definitions* and their readable labels (`meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, `meta.condition_labels`) live on the **test object** and are **not** reachable from an execution (no `test_id` is exposed). If you must spell out *which* rule fired, ask the user for the test id and `blazemeter_tests read` it — never raw kpi ids or op codes, and never invent the criteria.
- **Cross-account consent.** Each execution's account is consent-gated independently; both must have AI consent enabled, or Step 0 stops.
- **Pagination.** When picking executions from a test, `blazemeter_execution list` maxes at 50 per call — page by `offset` if the run you want is older than the first page.
