---
name: bzm-daily-digest
description: Sweep every BlazeMeter test that ran across an account (or a chosen workspace/project) in a time window (default last 24h) and produce ONE cross-test scorecard — per-test pass/fail, regression vs each test's own baseline, ranked incidents, and a prioritized "what needs your eyes today" list. Use when asked for a daily/standup digest, a morning rollup, an overnight summary, or a "what broke since yesterday?" view across many tests at once.
---

Produce the **daily digest**: one cross-test scorecard for a whole account — or a chosen workspace/project — over a window. Where `bzm-analyze-test` trends *one* test deeply and `bzm-triage-failure` diagnoses *one* run, this skill sweeps **every test that ran** in the window, judges each against **its own baseline** (not just absolute pass/fail), and rolls the whole portfolio up into a scoreboard, a ranked incident list, and a short "needs your eyes today" list. It is markdown/terminal-first — a scannable standup artifact, **not** a branded HTML report (reach for `bzm-report` when you want the shareable HTML).

**Division of labor (important):** the MCP is used for the *control plane* — resolving the account/scope interactively, the AI-consent gate, and any after-digest drill-in on a single run. The *bulk data pull* (every test's executions, every run's reports) is **never** done by chaining MCP calls — at real account sizes that is thousands of payloads. It is handed off to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py`, which sweeps the BlazeMeter API directly, does all the arithmetic (window filtering, baseline resolution, KPI deltas, normalization), and returns one compact pre-aggregated JSON. You read only that JSON and write the narrative.

## Step 0 — Resolve the account, ask the user the rollup scope, then census the tests

This is the **cross-test** variant of Context Resolution. A digest operates over **many tests at once**, so it resolves the **account**, then **asks the user how wide to roll up** — the **whole account** (every workspace → project → test), a **single workspace**, or a **single project**. It **never assumes** the breadth and never narrows to a single test. **Don't assume:** the user may belong to multiple accounts, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Resolve the account (tiered pick rule)

Resolve **only the account** here — which workspaces/projects are swept depends on the scope the user picks in Step 0b. Apply the uniform tiered pick rule to the account:

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

Check the resolved **account's** AI-consent state via `blazemeter_account read`. If the account has **not** consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` — before invoking the engine or fetching anything. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

### Step 0d — Census the window with `plan` (the practicality checkpoint)

Do **not** enumerate the test catalog — activity is what costs, so the census is **window-first**: one server-side-filtered listing tells you how many runs (across how many tests) fall in the window, even account-wide. It prints a small JSON to stdout (`runs_in_window`, `tests_ran`, a per-test run count):

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py plan --account-id <id> \
  --from <window start> --to <window end>       # ISO-8601 or epoch; defaults to the last 24h
# or:  --workspace-id <id>   |   --project-id <id>     (exactly one scope flag)
```

The engine reads the **same credentials the MCP uses** from the environment — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file). Never pass keys on the command line. If it exits with a credentials error, show the user which variables to set and stop.

**Practicality guard:** show the census to the user. The sweep's cost scales with the census (report fetches per run + a baseline lookup per active test) — hundreds of in-window runs is worth a heads-up and an offer to **narrow the scope or shorten the window** before proceeding. Never silently truncate the scope.

(If the user asked for a non-default window, resolve it — Step 1 — *before* running the census, so the census counts the right window.)

### Step 0e — Display the resolved scope and the census, then continue

Display the cross-test context block before acting, so the run is auditable:

```
Scope:      Whole account                                     ← or "Workspace <name> (ID)" / "Project <name> (ID)"
Account:    <account name>  (ID: <account_id>)
Window:     <resolved window, e.g. last 24h: 2026-06-26 09:00 → 2026-06-27 09:00>
Activity:   <N> runs across <M> tests in the window           ← from the plan census
```

Carry this resolved scope forward as **conversational memory** for later skills in the same conversation (display it, allow a one-step "switch"); **never persist it** to disk.

## Step 1 — Resolve the window

Default to the **last 24 hours** ending now. Let the user override in natural language — "since yesterday", "last 3 days", "this week", or an explicit date range ("2026-06-20 to 2026-06-26"). Compute a concrete `[from, to]` timestamp pair and **display it** (in Step 0e's block) so the user can see exactly what counts as "today".

## Step 2 — Run the sweep

One engine invocation does the whole bulk pull and all the deterministic judgment — listing each test's executions, keeping runs that overlap the window, fetching each kept run's reports, resolving each test's baseline, and computing the KPI deltas:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py sweep \
  --account-id <id> \                       # or --workspace-id / --project-id (exactly one)
  --from 2026-06-30T09:00:00Z --to 2026-07-01T09:00:00Z \
  --baseline-file .blazemeter/baseline.json \   # only if the user's repo has one
  --pins <scratch>/pins.json \                  # only if the user pinned baselines this conversation
  --out <scratch>/digest.json
```

- **`--baseline-file`** — pass the user's committed `.blazemeter/baseline.json` when the repo has one; its entries (a flat `{test_id: execution_id}` map) pin those tests' baselines.
- **`--pins`** — if the user pinned a baseline for specific tests earlier **in this conversation**, write those as a small JSON map `{"<test_id>": "<execution_id>"}` to a scratch file and pass it. Pins outrank the committed file. Omit otherwise.
- Baseline precedence per test is applied inside the engine: **conversational pin → committed file → last passing run** (from the test's own history, which may legitimately predate the window). A test with no passing run gets `"source": "none"` — no baseline is invented.
- Stdout is a **five-line summary** (tests swept, failures/regressions, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out`.
- **Exit codes:** `0` success; `2` usage/credentials (tell the user what to set/fix); `3` scope-level failure **or** too many fetch failures (default threshold 20%, tune with `--max-failure-rate`). On `3` the JSON may still exist — its `coverage` block says exactly what's missing; report the digest as **partial**, never as complete.

## Step 3 — Read the digest JSON and judge severity

Read `--out`. It is compact — one entry per test that ran (`runs_in_window` and `tests_ran` at the top level; idle tests are never fetched at all). Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. Per test you get:

- run counts: `runs_in_window`, `kpi_runs`, `passed` / `failed`, `skipped_partial` (aborted/errored runs whose KPIs were deliberately not folded in), `inconclusive`, `still_running`;
- `baseline` (`source`: `pin | file | last-passing | none`, and the execution id), `candidate_execution_id` (the newest failing run if any failed, else the newest run);
- `deltas` vs baseline (avg/p95/p99/throughput/error-rate, each with a `pct` and an `adverse` flag — adverse means a ≥10% move in the worse direction; throughput is judged per-virtual-user when the load config changed, flagged `normalized_per_vu`), `worst_kpi_move`, `regressed`;
- `notes` (`baseline_is_only_run`, `no_baseline`, `baseline_kpis_unavailable`, `candidate_kpis_unavailable` — the last also covers run types with no load KPIs, e.g. GUI/EUX runs), `anomaly_status`, and `incident_candidates` — the raw material for the incident list (`failure`, `regression`, `error_spike`, `endpoint_error_spike`, `anomaly`).

Your contribution is **judgment and prose**, ranking the incident candidates across all tests by severity:

1. **Outright failures** — a run that failed its criteria. Highest severity.
2. **Large regressions vs baseline** — a still-green run that moved a KPI well past 10% (the bigger the move and the more tests affected, the higher).
3. **Error-rate spikes** — overall error rate past 1% (and especially past 5%), or an endpoint erroring at near-100% even on modest traffic.
4. **Anomalies** — weight a KPI/label that recurs across **multiple tests or runs** as **systemic** (higher) over a lone one-off (lower / likely noise).

Treat `statistics_unavailable` as **insufficient data, not a finding** (never an incident, never "clean"); `inconclusive` runs are inconclusive, not green.

## Step 4 — Handle the edge cases gracefully

- **Empty window (nothing ran):** `tests_ran: 0` → **do not** fabricate a scoreboard. Emit the short empty-window form: confirm the scope and window, state plainly that **nothing ran in this window**, and stop. An empty digest is a valid, useful answer.
- **Partial coverage:** surface the `coverage` block honestly — skipped partial runs, failed fetches (with counts), anomaly stats unavailable. Never present a partial sweep as complete.
- **No baseline for a test:** its regression column reads "no baseline"; judge it on absolute pass/fail only.
- **Drill-ins stay interactive:** when the user asks about one incident ("what happened in run 9101?"), that is a *single-run* question — answer it with the MCP (`blazemeter_execution read`, `read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure`). Don't re-run the sweep for it.

## Output template

```
## BlazeMeter Daily Digest — <scope name> (Account/Workspace/Project ID: <id>)
**Window:** <from> → <to>   |   **Runs in window:** N   |   **Tests ran:** M   |   **Account:** <account name> (<account_id>)

### TL;DR — what needs your eyes today
1. <highest-severity item — test/run + one-line why>
2. ...
(3–7 prioritized items; the "if you read nothing else" list)

### Scoreboard
| Test | Runs | Pass/Fail | Newly regressed? | Worst KPI move | Baseline source |
|------|------|-----------|------------------|----------------|-----------------|
| <test name> (id) | 4 | 3/4 | ⚠ yes | p95 +34% vs baseline | last-passing |
| ...              |   |     | ok    | —                    | committed file  |
(failures first, then largest regressions — the JSON is already sorted this way; idle tests omitted here — see footer)

### Top incidents (ranked by severity)
1. **[FAIL]** <test> run <exec_id> — failed criteria; error rate 26.7% (baseline 0.4%).
2. **[REGRESSION]** <test> run <exec_id> — p95 480ms → 642ms (+34%) vs baseline <baseline_id>; still green.
3. **[ERROR SPIKE]** <test> run <exec_id> — /checkout 98% errors on 120 samples.
4. **[ANOMALY · systemic]** <KPI/label> recurred across <N> tests.
...

### Coverage notes
- Skipped (partial/aborted) runs: <N> — KPIs not folded in
- Tests with no baseline: <N> — judged on absolute pass/fail only
- Fetch coverage: <ok>/<attempted> (<failed> failed)   ← only when failures > 0
- Normalized for load-config change: <tests, if any>
```

For an **empty window**, collapse the body to:

```
## BlazeMeter Daily Digest — <scope name> (<id>)
**Window:** <from> → <to>

Nothing ran in this window. No executions to report.
```

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_*` list/read calls per test and per execution burns enormous time and tokens at real account sizes and is exactly what the engine exists for. MCP is for Step 0's interactive picks, the consent gate, and single-run drill-ins afterward — nothing in between.
- **Ask the scope; census the window, don't walk the catalog.** Step 0 resolves the **account**, **asks** the rollup breadth (whole account / a workspace / a project — never assumed), and runs `plan` for the **window census** — activity is the cost driver, and idle tests are never touched. A big census is a reason to *offer narrowing*, never to silently truncate.
- **Consent before sweep.** The AI-consent check (Step 0c) must pass before any `plan`/`sweep` invocation — the gate lives in the MCP layer, and the engine assumes it already happened.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the digest, or in the conversation.
- **Trust the engine's arithmetic.** Deltas, normalization, baseline choice, and status buckets are computed deterministically and fixture-tested. Your job is severity ranking and narrative — if a number looks wrong, say so and show it; don't silently recompute.
- **The engine already excludes partial runs.** Aborted/errored runs are counted (`skipped_partial`) but their KPIs never fold into the scoreboard — keep the digest honest by reporting the count.
- **`statistics_unavailable` is not a finding.** It means anomaly stats couldn't be read (run too short, or the stats endpoint unavailable) — insufficient data, never "anomalies detected" and never a clean bill.
- **Don't compare a run to itself.** A green run is never its own baseline — last-passing resolution excludes the candidate, so a still-green regression is detectable whenever any prior pass exists. `baseline_is_only_run` in a test's notes means a *pinned* baseline (conversational or committed file) points at the candidate itself — report "baseline run, no prior to compare", not a 0% move.
- **Failure criteria live on the test, not the execution.** At digest altitude, lead with the overall verdict and the deltas; reach into `blazemeter_tests read` (failure criteria + labels) only when explaining *why* a specific run failed during a drill-in.
- **Never persist scope.** The resolved account/workspace/project is conversational memory only. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing. Scratch files (`pins.json`, `digest.json`) go in the session scratch directory, not the user's repo.
