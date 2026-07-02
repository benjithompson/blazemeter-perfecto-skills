---
name: bzm-test-analysis
description: Analyze one BlazeMeter test's executions — trend its run history over a time window, compare a candidate run against the resolved baseline, diff two specific executions with a ship/no-ship verdict, or show/pin the active baseline — delivered as a prose assessment or a branded, self-contained HTML report. Use when asked to analyze, review, trend, or report on a test's performance history, regressions, error patterns, or SLA compliance; to compare two runs or check a run against a baseline; to resolve, show, or pin a test's baseline; or for a shareable multi-run HTML trend/regression report.
---

Analyze the executions of **one** BlazeMeter test and deliver a QA performance engineering assessment. One skill, four modes — pick by what the user asked:

| Mode | Question it answers | Data pull |
|------|--------------------|-----------|
| **Trend** | "How has this test been doing over time?" | `history` (many runs over a window) |
| **Baseline-compare** | "Did this run regress against the baseline?" | resolve baseline, then `run-pair` |
| **Pair-compare** | "Compare run X and run Y — ship or no-ship?" | `run-pair` (two explicit executions) |
| **Baseline-lookup** | "What is the baseline for this test, and why?" (show / resolve / pin) | MCP only |

Every analysis mode can deliver its result as **prose in the chat** (the default) or as a **branded, self-contained HTML report** written to `./bzm-reports/` (when asked for something shareable). Writing or updating the committed CI baseline file is **not** this skill — hand that to `bzm-set-baseline`.

**Division of labor (important):** the MCP is used for the *control plane* — resolving the test or executions interactively, the AI-consent gate, the test object's failure criteria, and single-run drill-ins afterward. The *bulk data pull and all the arithmetic* — listing runs, fetching reports, window filtering, per-run KPIs, baseline resolution, deltas, normalization, regression flags — is **never** done by chaining MCP calls; it is handed to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py` (`history` for many runs, `run-pair` for two), which fetches the BlazeMeter API directly and returns one compact pre-aggregated JSON. You read only that JSON and contribute judgment and prose.

## Choosing the mode and output format

Infer the mode from the request — ask only when it's genuinely ambiguous:

- "compare runs 123 and 456", "did run X regress vs run Y" → **Pair-compare**.
- "compare against the baseline", "did the latest run regress?", "gate this release" → **Baseline-compare**.
- "trend", "over time", "history", "how's it been doing", or any time window → **Trend**.
- "what's the baseline", "show/resolve the baseline", "baseline against run 123" (a pin) → **Baseline-lookup**.
- Generic ("analyze my checkout test") → present a **choice list** of the four modes (one line each) and let the user pick. Don't guess.

Output format is orthogonal and inferred the same way: "report", "HTML", "shareable", "send to stakeholders" → produce the **HTML report** (Step 4-HTML); otherwise deliver **prose** and don't ask. A request to *write/update* the committed baseline file is neither — route it to `bzm-set-baseline`.

## Step 0 — Resolve and confirm context

Always resolve and **display** the full context (with ids) before doing any analysis, so the user can confirm you're operating on the right thing. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. An analysis of — or a comparison against — the wrong thing is worse than none.

### Step 0a — Identify the target(s)

**Trend / Baseline-compare / Baseline-lookup** target a **test**; **Pair-compare** targets **two executions** (a **baseline** and a **candidate** — establish which is which up front; the diff direction depends on it, and they must be two *different* executions).

- **An id was given** (`test_id`, or an `execution_id` per side in pair mode) → trust it and resolve *upward* (Step 0b). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - **Pair mode without execution ids:** once the test is confirmed, list its executions with `blazemeter_execution list { test_id, limit: 50, offset: 0 }` (page by `offset`) and let the user pick the baseline and the candidate. A common shape is **most-recent run as candidate, the prior passing run as baseline** — offer that as a confirmable suggestion, never a silent default.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

### Step 0b — Resolve the full hierarchy upward and confirm

**Test-target modes** — chain these calls; each response provides the ID needed for the next:

```
1. blazemeter_tests read         { test_id: <id> }
   → captures: test name, project_id, failure criteria (rules + meta.* labels — used for SLA)

2. blazemeter_project read       { project_id: <project_id from step 1> }
   → captures: project name, workspace_id

3. blazemeter_workspaces read    { workspace_id: <workspace_id from step 2> }
   → captures: workspace name, account_id

4. blazemeter_account read       { account_id: <account_id from step 3> }
   → captures: account name, AI-consent state
```

**Pair mode** — do the equivalent chain **for each of the two executions**, starting one level lower:

```
1. blazemeter_execution read   { execution_id: <id> }
   → captures: execution_status, ended (completion), project_id, execution_name
   (Use execution_name as the run's display name. The execution API does NOT expose test_id,
    load config, or failure-criteria detail — so there is no blazemeter_tests read on this path,
    and a missing test_id here is expected, not a failure.)
2–4. blazemeter_project read → blazemeter_workspaces read → blazemeter_account read, as above.
```

- **Completion gate (pair mode):** an execution is only comparable once finished — `ended` must be **NOT null** (`ended == null` ⇒ still running). If either run is still running, stop and say which one isn't done; comparing a partial run produces misleading KPIs.
- **Cross-account / cross-test pairs:** the two executions need not share an account, workspace, project, or even test. Resolving both hierarchies independently is what surfaces this. If they differ, that's allowed but **call it out explicitly** in the output and disambiguate same-named entities by their ids.

**AI Consent gate:** if an account has **not** enabled AI consent (from the `blazemeter_account read`), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding. In pair mode check **each** execution's account. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

Present the resolved context to the user before continuing — for test-target modes:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

— and in pair mode the same block **per execution** (headed `Baseline execution: <id>` / `Candidate execution: <id>`, with `Test: <execution_name>` since the execution API doesn't expose the test id).

If any link in a chain fails (e.g. a `project_id` missing from a response, or a 403), **stop and report the gap** — do not proceed against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Resolve the window (Trend mode only)

Default to the **last 30 days** ending now — enough runs for a trend without pulling years of history — and **say so** in the output ("analyzed the last 30 days — ask for 'all history' to widen"). Let the user override in natural language — "last quarter", "since the 2.3 release", "all of June", "all history", or an explicit date range. Compute a concrete `[from, to]` timestamp pair and **display it** alongside the context block. If the window turns out to contain no runs (Step 3), offer to widen it rather than fabricating an analysis. "All history" is honored via a wide-open `--from`; note the engine's history cap (Gotchas) when it may bite.

## Step 2 — Pull the data with the engine

### Trend: one `history` invocation

One engine invocation does the whole bulk pull and all the deterministic judgment — listing the test's executions, keeping runs that overlap the window, bucketing them by verdict, fetching each complete run's summary KPIs, resolving the baseline, and computing each run's deltas against it:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py history \
  --test-id <test_id> \
  --from 2026-06-01T00:00:00Z --to 2026-07-01T00:00:00Z \
  --baseline-file .blazemeter/baseline.json \   # only if the user's repo has one
  --pins <scratch>/pins.json \                  # only if the user pinned a baseline this conversation
  --out <scratch>/history.json
```

- **`--baseline-file`** — pass the user's committed `.blazemeter/baseline.json` when the repo has one; an entry for this test pins its baseline.
- **`--pins`** — if the user pinned a baseline for this test earlier **in this conversation**, write it as a small JSON map `{"<test_id>": "<execution_id>"}` to a scratch file and pass it. Pins outrank the committed file. Omit otherwise.
- Baseline precedence is applied inside the engine: **conversational pin → committed file → last passing run** (from the test's own history, which may legitimately predate the window). A test with no passing run gets `"source": "none"` — no baseline is invented.
- Stdout is a **five-line summary** (runs in window, pass/fail, baseline, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out`.
- **Exit codes:** `0` success; `2` usage/credentials (tell the user what to set/fix); `3` scope-level failure (e.g. a bad test id) **or** too many fetch failures (default threshold 20%, tune with `--max-failure-rate`). On `3` the JSON may still exist — its `coverage` block says exactly what's missing; report the analysis as **partial**, never as complete.

### Baseline-compare: resolve the baseline, then `run-pair`

First resolve the **candidate** (the run being judged — usually the newest, or the one the user named) and the **baseline**, in precedence order **conversational pin → committed file → last passing run**:

1. **Conversational pin** — if the user pinned an `execution_id` earlier in this conversation, that is the baseline.
2. **Committed CI file** — if the repo has a `.blazemeter/baseline.json`, read its entry with the shared script (it parses, normalizes ids, and surfaces a malformed file as an error rather than guessing):

   ```bash
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py resolve \
     --file .blazemeter/baseline.json --test-id <test_id>
   ```

   A **malformed** file exits non-zero — report it and stop, don't fall through silently. (A *missing* file is just an empty baseline, not an error.)
3. **Last passing run** — list the test's executions (`blazemeter_execution list { test_id, limit: 50, offset: 0 }`, page by `offset` only if the first page has no pass; capture each run's `id`, `status`, `end_time`) and let the script pick the most recent passing one — excluding the candidate itself:

   ```bash
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py last-passing \
     --executions <scratch>/executions.json
   ```

   If it returns `null`, there is **no passing run** to baseline against — say so plainly and stop (don't silently pick a failed run). Only a clean `pass` counts; `unset`, `abort`, `error`, `noData`, and still-running runs are excluded.

Then compare with **baseline = the resolved execution** and **candidate = the run being judged**, exactly as pair mode below.

**Pinning** ("baseline against execution 98765"): validate the id first — `blazemeter_execution read` must show `ended != null` (never baseline a still-running run), a clean `pass` (warn and get confirmation before pinning a non-passing run as the bar), and a `project_id` matching the test's (a mismatch means you may be about to baseline a run from a different test — stop and report it). A pin is **conversational memory only — never persisted, never written to disk**. To make a baseline durable for CI, hand off to `bzm-set-baseline`.

### Pair-compare: one `run-pair` invocation

One engine invocation fetches both runs' reports and does every piece of arithmetic — summary KPIs, per-endpoint request stats, anomaly status, all deltas, load normalization, and the regression flags:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py run-pair \
  --baseline-id <baseline_execution_id> \
  --candidate-id <candidate_execution_id> \
  --out <scratch>/compare.json
```

- Stdout is a **five-line summary** (statuses, KPI availability, adverse moves, fetch coverage, output path) — show it as progress. The full result is the JSON at `--out` (a session scratch path, never the user's repo). `--no-anomalies` skips the anomaly-status fetches if the user doesn't want them.
- **Exit codes:** `0` success; `2` usage error — including the **same-id guard** (baseline and candidate are the same execution: go back and pick a real baseline) and missing credentials; `3` an execution itself couldn't be read (bad id or no access — report it, don't guess). Missing *sub-reports* never fail the run: they degrade into the JSON's `coverage` block and `notes`.

**Credentials (all engine invocations):** the engine reads the **same credentials the MCP uses** from the environment — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file). Never pass keys on the command line. If it exits with a credentials error, show the user which variables to set and stop.

### Baseline-lookup: MCP only

No engine invocation. Resolve the baseline exactly as in Baseline-compare above (pin → committed file → last passing), then pull the baseline execution's KPIs so the user can sanity-check it:

```
blazemeter_execution read_all_reports  { execution_id: <baseline_id> }
```

Use the **summary** sub-report for the headline KPIs (avg / p90 / p95 / p99 response time, throughput RPS, error rate, achieved peak concurrency). Present *which* execution is the baseline, **why** (pinned this conversation vs. committed file vs. last-passing), and its KPIs (Output templates below).

## Step 3 — Analyze

Read the engine's JSON. Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. Your contribution is judgment and prose.

### 3A. Trend mode — read `history.json` through six lenses

The JSON is compact — one entry per run in the window, **oldest first** (the trend axis). You get: top-level counts (`runs_in_window`, `kpi_runs`, `passed`/`failed`, `skipped_partial`, `inconclusive`, `still_running`); `baseline` (`source`: `pin | file | last-passing | none`, and the execution id), `baseline_kpis`, `candidate_execution_id` (the newest failing run if any failed, else the newest run), `regressed_runs`; per run in `runs[]`: `execution_id`, `started`/`ended` (epoch seconds), `report_status`, `bucket`, `kpis` (avg/p90/p95/p99 ms, hits, throughput RPS, error-rate %, max users, RPS-per-VU, duration), `deltas` vs the baseline (avg/p95/p99/throughput/error-rate, each with a `pct` and an `adverse` flag — adverse means a ≥10% move in the worse direction; throughput is judged per-virtual-user when the load config changed, flagged `normalized_per_vu`), `worst_kpi_move`, `regressed`, `is_baseline`, `anomaly_status`, and `anomalies` (KPI + label) when present; plus `incident_candidates` (`failure`, `regression`, `error_spike`, `endpoint_error_spike`) and `notes` (`no_baseline`, `baseline_is_only_run`, `baseline_kpis_unavailable`; per-run `kpis_unavailable` also covers run types with no load KPIs, e.g. GUI/EUX runs).

Work through these lenses in order:

1. **Trend narrative** — walk `runs[]` chronologically. Improving, stable, or degrading? Point at the specific runs where a KPI moved — a one-run blip reads differently from a three-run slide. Use each run's `deltas` and `worst_kpi_move`.
2. **Response-time distribution health** — are p90/p95/p99 tracking proportionally? A widening gap (p99 climbing while avg is flat) signals tail-latency problems. Name the worst and best runs.
3. **Error-rate pattern** — trend across runs, and any run past 1% (incident) or 5% (severe — both already flagged as `error_spike` incidents). An `endpoint_error_spike` incident names a label erroring at near-100% on the newest problem run — the first place to point a developer.
4. **Throughput & scalability** — RPS trend over time. When `max_users` differs between runs, compare `rps_per_vu` instead of raw RPS (the engine's deltas already do, flagged `normalized_per_vu`) and say so.
5. **Anomaly recurrence** — count runs by `anomaly_status`. A KPI/label pair recurring in the `anomalies` of **3+ runs** is a systemic signal, not noise; a lone one-off is likely noise. Treat `statistics_unavailable` as **insufficient data, not a finding**.
6. **SLA / failure-criteria compliance** — per-run pass/fail is `report_status`. The criteria **definitions** and readable labels come from the **test object** (Step 0b `blazemeter_tests read` — render the readable labels, never raw KPI ids or op codes). No per-criterion per-run results exist, so attribute *which* criterion drove a failure by comparing the failing run's `kpis` against the test's thresholds — and say it's an inference. Report the pass rate and the criteria most often implicated.

### 3B. Compare modes (baseline-compare and pair-compare) — read `compare.json`, then verdict

The shape: `baseline` / `candidate` (per run: `execution_id`, `name`, `test_id` when the API exposes it, `report_status` (`pass | fail | unset | abort | error | noData`), `created`/`ended`, `still_running`, `kpis` — or `null` when the run has no load KPIs — and `anomaly_status`); `kpi_deltas` (candidate vs baseline per KPI — `avg`, `p95`, `p99`, `throughput`, `error_rate` — each with a `pct` and an `adverse` flag; **adverse = a ≥10% move in the worse direction**, with two subtleties: **load normalization** — when the runs' achieved peak concurrency differs, throughput is judged on RPS-per-VU, flagged `normalized_per_vu: true`; and **error rate from a clean baseline** — a 0% baseline makes a relative change undefined, so the delta carries `pct: null` and a `points` value, adverse when it crosses 1%); `endpoints` (per-label deltas for labels present in both runs — `matched`, sorted worst-first — plus `baseline_only` / `candidate_only` lists); `verdict_inputs` (`candidate_failed_while_baseline_passed`, `adverse_kpi_moves`, `worst_kpi_move`, `regressed`, `load_config_differs`, `endpoints_with_adverse_moves`); `notes` (honesty flags — `*_kpis_unavailable`, `*_request_stats_unavailable`, `*_still_running`); and `coverage` (fetch bookkeeping — surface failures honestly; never present a partial compare as complete).

Decide the verdict from `verdict_inputs` and the notes:

- **NO-SHIP** if any of: `candidate_failed_while_baseline_passed`; `error_rate` adverse (past 10% relative, or crossed 1% in points from a clean baseline — call out 5%+ as severe); `p95` or `p99` adverse.
- **SHIP** if `regressed` is false, the candidate's `report_status` is `pass` (or `unset` with everything else clean — but say the pass/fail signal is indeterminate), and coverage is clean.
- **SHIP WITH CAVEATS / INCONCLUSIVE** if the only adverse moves are latency/error-rate shifts while `load_config_differs` is true (higher load legitimately changes them — only throughput is normalized; say what would make it conclusive, e.g. re-run at the baseline's concurrency), or KPIs/sub-reports are missing, or either run's `report_status` is `abort`/`error`/`noData`/`unset`.

Weight the KPIs: error rate usually dominates, then p95/p99 tail latency, then throughput, then avg. Use `endpoints.matched` to name the offending endpoint(s) — a run-level regression concentrated in one label is a sharper finding than a diffuse one. Always give **reasons tied to specific numbers**, leading with the single most decision-relevant KPI.

## Step 4 — Deliver

### Prose output — Trend mode

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

### Prose output — Compare modes

```
## BlazeMeter Run Comparison
**Baseline:**  exec <baseline_id> — <name> (<date>)  <source, when resolved: pinned | committed file | last-passing>
**Candidate:** exec <candidate_id> — <name> (<date>)
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

### Regressions
- <KPI>: <baseline> → <candidate> (<Δ% or points>), past threshold. <one-line consequence>
(or: "None past threshold.")

### Notes / Caveats
- <config mismatch, KPIs unavailable, coverage gaps, cross-account/cross-test, still-running>
```

### Prose output — Baseline-lookup

```
## BlazeMeter Baseline: <test name> (test ID: <test_id>)

### Active baseline
- Execution: <execution_id> — <execution_name> (<ended date>)
- Source:    pinned (this conversation) | committed file (.blazemeter/baseline.json) | last passing run
- Status:    <execution_status>   Completed: <ended>

### Baseline KPIs
| KPI | Value |
|-----|-------|
| Avg RT (ms) | | p90 / p95 / p99 (ms) | | Throughput (RPS) | | Error rate (%) | | Peak concurrency | |

### Notes
- <e.g. "no passing run found — nothing to baseline", non-passing pin confirmed by user,
   pin is conversational only and won't persist, malformed baseline file>
→ To make this baseline durable for CI, use bzm-set-baseline to write .blazemeter/baseline.json.
```

### HTML output (any analysis mode, on request)

The Report is a single shipped HTML template that renders itself from a data model in the browser — **no Python step and no local interpreter**.

**1. Build the data model** — a single JSON object with `meta` / `summary` / `runs` / `regressions` / `sla` / `endpoints`. **Supply `generated_at` yourself** (current time, ISO 8601) — the template never reads the clock. Put **no credentials** anywhere in it.

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

**Trend mode mapping** (from `history.json`): include every run whose `bucket` is `kpi` as a `runs[]` row, oldest first (partial/inconclusive/running runs stay out — report their counts in the coverage notes). The field names differ, so map explicitly: `execution_id`→`execution_id`; `ended` (epoch **seconds**) → `timestamp` (**convert** to ISO 8601 UTC) and derive a short `label` (e.g. `Jun 24 15:36`); `report_status`→`status`; `kpis.avg_ms`→`kpis.avg_rt_ms`, `p90_ms`→`p90_ms`, `p95_ms`→`p95_ms`, `p99_ms`→`p99_ms`, `throughput_rps`→`rps`, `error_rate_pct`→`error_rate_pct`, `max_users`→`concurrency`. A run noted `kpis_unavailable` keeps its row with `kpis: {}` — the template renders dashes, never fabricated zeros. Build one `regressions[]` entry per **adverse** delta on the runs that matter (at minimum every `regressed` run; lead with the candidate): `kpi` a readable name (`avg` → `avg response time`, …), `from_value`/`to_value` from `baseline_kpis` and the run's KPI, `pct_change` the delta's `pct` verbatim (an `error_rate` delta with `pct: null` carries `points` — put the points move in the `note`), `direction` `up` for a latency/error rise / `down` for a throughput fall, `severity` `critical` when the run also failed or breaches an SLA threshold, else `warning`; note `normalized_per_vu` comparisons ("per-VU — load config changed") and anything from `incident_candidates`. If `baseline.source` is `none`, leave `regressions` empty and say in the narrative that no baseline comparison was possible. `sla.pass_count`/`fail_count` are the top-level `passed`/`failed`; `sla.rules[]` from the test's `failure_criteria` using its `meta.*` labels (never raw kpi ids or op codes), with each rule's `pass_rate_pct` inferred by comparing runs' KPIs to the threshold — and say it's an inference in the `note`. **Endpoint hot spots are one MCP drill-in on the latest run only**: `blazemeter_execution read_all_reports` for the history's `candidate_execution_id`, rank its `request_stats[]` by `percentile_95_ms` and error contribution, map `label_name`→`name`, `percentile_95_ms`→`p95_ms`, `errors_rate_percent`→`error_rate_pct`; set `trend` only with evidence (an `endpoint_error_spike` incident → `degrading`), else omit. Do **not** page endpoint stats across all runs.

**Compare modes mapping** (from `compare.json`): the same model, two rows — `runs[0]` the baseline, `runs[1]` the candidate (same KPI field mapping; a `kpis: null` side keeps its row with `kpis: {}`). `meta.title`: `"<name> — Baseline vs Candidate"`; `summary.verdict` from Step 3B. One `regressions[]` entry per adverse `kpi_delta` (`from_value` = baseline KPI, `to_value` = candidate KPI, `run_id` = the candidate). `endpoints[]` from `endpoints.matched` worst-first (candidate-side values; `trend: "degrading"` for labels with adverse moves). Omit `sla` (per-criterion rules live on the test object, which pair mode may not have resolved). Surface a CONFIG MISMATCH prominently in the narrative.

**2. Fill the template**: read `${CLAUDE_PLUGIN_ROOT}/shared/assets/report-template.html` (it bakes in the CSS, the approximated-BlazeMeter brand vars, and the vendored client-side JS that builds every section and the trend charts from `runs[]`). Serialize the data model to JSON and **replace the single token `{{REPORT_DATA_JSON}}`**. The token sits inside `<script>window.REPORT_DATA = {{REPORT_DATA_JSON}};</script>`, so before substituting, **HTML-escape every `</` in the JSON to `<\/`** — the one transform that guarantees a string value (e.g. an endpoint label like `</checkout>`) can never close the `<script>` tag early. Substitute the token literally; do not otherwise reformat the template.

**3. Write the result** as a `.html` file (default `./bzm-reports/`, filename a slug of the test name + `generated_at`). Use the `Write` tool — no shell, no `python`. The output is fully self-contained (offline, no CDN — safe to email). Then tell the user:

```
## BlazeMeter Report: <test name>
**Window / Runs / Baseline / Verdict:** <as applicable to the mode>
**Report file:** <path to the HTML>

### Highlights
- <top regression or trend, with numbers>   - <SLA compliance>   - <worst endpoint hot spot>

### Coverage notes                                  ← only when something is missing
- Fetch coverage: <ok>/<attempted> (<failed> failed) — report is partial

Open the HTML file to see the full branded report.
```

## Step 5 — Drill-ins stay interactive

When the user asks about one run afterward ("what happened in run 82525951?", "which endpoints were slow?", "*why* did the candidate fail?"), that is a *single-run* question — answer it with the MCP (`blazemeter_execution read`, `read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure` for a failed run). Don't re-run the bulk pull for it, and don't page MCP reports across many runs — that's what Step 2 was for. To spell out *which* failure criterion fired when only executions were resolved (pair mode), the criteria definitions live on the **test object** — ask for the test id and `blazemeter_tests read` it; never invent criteria.

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_execution list` + per-run report reads burns enormous time and tokens on a busy test and is exactly what the engine exists for. MCP is for Step 0's interactive picks, the consent gate, the test object's criteria, and single-run drill-ins — nothing in between.
- **Consent before pull.** The AI-consent check (Step 0b) must pass before any engine invocation — the gate lives in the MCP layer, and the engine assumes it already happened.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the data model, in the generated HTML (it's meant to be emailed), or in the conversation.
- **Trust the engine's arithmetic.** Deltas, direction, normalization, baseline choice, and status buckets are computed deterministically and fixture-tested. Your job is narrative, mapping, and severity judgment — if a number looks wrong, say so and show it; don't silently recompute or re-fetch.
- **The engine already excludes partial runs.** Aborted/errored runs are counted (`skipped_partial`) but their KPIs never fold into a trend — including them would distort it. Report the count.
- **`kpis_unavailable` / `kpis: null` is not a zero.** Some run types (e.g. GUI/EUX) report no load KPIs at all — such a run shows as "—" (or an empty `kpis` row in HTML), never a 0 ms / 0% row. In a compare with either side null there are no `kpi_deltas`; the verdict rests on `report_status` alone and should say so.
- **`statistics_unavailable` is not a finding.** Anomaly stats couldn't be read (run too short for the anomaly engine, or the endpoint unavailable) — insufficient data, never "anomalies detected" and never a clean bill.
- **Don't compare a run to itself.** A green run is never its own baseline — last-passing resolution excludes the candidate, and the `run-pair` engine refuses equal ids (offer a different baseline instead of retrying). `baseline_is_only_run` in the notes means a *pinned* baseline points at the newest run itself — report "baseline run, no prior to compare", not a 0% move.
- **Load-config changes break raw comparisons.** `load_config_differs` / differing `max_users` means throughput is already normalized per-VU (`normalized_per_vu`); latency and error rate are **not** normalizable across load levels — report their raw diffs with a prominent CONFIG MISMATCH warning and lower verdict confidence rather than pretending they're comparable. Only *achieved* peak concurrency is visible — the configured shape lives on the test object, so don't claim to compare it.
- **Indeterminate failure status.** `report_status` can be `unset` (no criteria ⇒ no pass/fail signal), `abort`, `error`, or `noData` — none are a clean "pass". Don't read `unset` as "passed"; surface it as indeterminate.
- **Tiny absolute values, huge percentages.** Error rate going 0.02% → 0.06% is +200% relatively but operationally trivial. Show absolute values alongside percentages; conversely, flag any absolute crossing of 1% / 5% even when the relative move is small.
- **Endpoint sets can shift.** `baseline_only` / `candidate_only` labels mean the runs didn't exercise the same endpoints — often a script change. A "regression" on a barely-overlapping endpoint set is really a scenario change.
- **Failure criteria live on the test, not the execution.** Per-run responses carry only the overall verdict; definitions and readable labels come from `blazemeter_tests read`. Infer per-run criterion outcomes from KPIs vs thresholds, and say it's an inference.
- **Completion before comparison.** Step 0's `ended != null` gate is the real check; the JSON's `still_running` flag is a backstop. A still-running execution returns partial KPIs that look like a regression — and must never be pinned as a baseline.
- **"Passing" is an explicit pass.** Baseline selection counts only a clean pass verdict; `unset`, `abort`, `error`, `noData`, and still-running runs are excluded. If nothing passes, there is no baseline — say so rather than baselining a failed run.
- **Malformed / missing baseline file.** A *missing* `.blazemeter/baseline.json` is an empty baseline (not an error). A *present but malformed* file is a real error — the script exits non-zero; report it and ask the user to fix the file, don't silently fall through to last-passing.
- **Pin ≠ committed file.** A conversational pin lives only for this conversation and is **never** written to disk; the committed `.blazemeter/baseline.json` is written only by `bzm-set-baseline` at the user's explicit request. Don't conflate them.
- **Deep windows can hit the history cap.** The engine pages executions newest-first (50 per page, up to 20 pages ≈ 1,000 runs) and stops once a page predates the window. A hyperactive CI test can have its **oldest in-window runs truncated** — especially on an "all history" pull. If `runs_in_window` looks suspiciously flat for a very active test, say the history may be capped and offer a shorter window.
- **Field-name mapping is exact (HTML).** The engine JSON says `avg_ms` / `throughput_rps` / `max_users`; the data model says `avg_rt_ms` / `rps` / `concurrency`. Map deliberately — a mis-key silently drops a KPI from the charts. Timestamps are epoch **seconds** in the JSON and ISO 8601 in the model — convert.
- **`generated_at` is supplied, not read.** The template never reads the clock (so the render is deterministic). `meta.title` and `meta.generated_at` are required — omit them and the header renders blank.
- **Escape `</` before substituting (HTML).** The data model is injected into a `<script>` tag — any `</` inside a string value must become `<\/` first. This is the only transform the JSON needs.
- **The template is the source of layout/branding.** Don't hand-write report HTML or build sections yourself — always fill `${CLAUDE_PLUGIN_ROOT}/shared/assets/report-template.html` so every report is consistent and on-brand.
- **Pagination.** `blazemeter_execution list` maxes at 50 per call — page by `offset` if the run you want is older than the first page.
- **Never persist scope.** The resolved account/workspace/project/test is conversational memory only. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing. Scratch files (`pins.json`, `history.json`, `compare.json`, `executions.json`, the working data model) go in the session scratch directory, not the user's repo; only a final `.html` lands where the user asked.
