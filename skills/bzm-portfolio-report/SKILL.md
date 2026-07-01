---
name: bzm-portfolio-report
description: Generate ONE branded, self-contained HTML scorecard across MANY BlazeMeter tests in a workspace or project over a window (default a quarter) — per-test health, SLA-compliance %, trend arrows, and regression-vs-own-baseline flags, plus ranked incidents. Use when asked for a shareable/stakeholder portfolio report, a quarterly or release scorecard across a whole suite, an executive cross-test rollup, or a "how is the whole portfolio doing?" HTML you can email.
---

Produce the **Portfolio Report**: a single branded, self-contained HTML scorecard that rolls up **every test in a scope** (a workspace or project) over a window (default a quarter) — each test's health, SLA-compliance %, trend, and whether it regressed against **its own baseline**, plus a ranked cross-test incident list. It is the **shareable HTML rendering** of the same cross-test view `bzm-daily-digest` produces in markdown: reach for the digest when you want a scannable standup artifact in the terminal, and for this skill when you want a stakeholder-facing, emailable HTML scorecard. Where `bzm-report` trends **one** test across its runs, this skill is its **portfolio sibling** — the same template, the same brand, one row per test instead of one row per run.

**Division of labor (important):** the MCP is used for the *control plane* — resolving the account and scope interactively, the AI-consent gate, and any after-report drill-in on a single run. The *bulk data pull* (every test's executions, every run's reports, every baseline comparison) is **never** done by chaining MCP calls — at a quarter's depth across a whole suite that is thousands of payloads. It is handed off to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py`, which sweeps the BlazeMeter API directly, does all the arithmetic (window filtering, baseline resolution, KPI deltas, normalization), and returns one compact pre-aggregated JSON. You read only that JSON, map it into the portfolio Report data model, and fill the shipped HTML template — the render is a token replacement plus a file write, no local interpreter.

## Step 0 — Resolve the account, choose the rollup scope, then census the tests

This is the **cross-test** variant of Context Resolution. A portfolio report operates over **many tests at once**, so it resolves down to a **scope** (a workspace, or a project within it) — it does **not** narrow to a single test. Every don't-assume guarantee of single-test resolution still applies; only the final level changes. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Resolve account → workspace (→ project) with the tiered pick rule

Apply the uniform tiered pick rule at **each** level you must resolve:

- Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → **display** it and proceed; more than one → present the pick and **stop** for the user's choice (never silently take the default).
- To enumerate options, list one page (`blazemeter_account list` / `blazemeter_workspaces list` / `blazemeter_project list`, `limit: 50`).
  - **Fits a choice list** (small set — the first page is *not* full) → present an **interactive choice list**, every entry showing name + id (default marked), the user clicks one; if there are more options than the choice widget holds, fall back to a **numbered text list** with ids (e.g. `1. Acme (account 12345)`).
  - **Too big / paginated** (the first page comes back full → more pages exist, e.g. >50) → **don't dump it**; ask the user to **name, paste an id, or filter**. A pasted **id short-circuits** any level via a direct `read`; a **name** you resolve by paging and matching.
- Always show the **id** next to each name so same-named entities are distinguishable.
- **Name doesn't resolve cleanly:** no match → say so, show what *is* available, stop; multiple matches → list each candidate with its **parent and id** and let the user pick; 403 → report the access gap, don't retry. **Never fall back to the default** at any level.

### Step 0b — Choose the scope to roll up over

The portfolio rolls up over **one scope**:

- **Project** (default) — roll up the tests in the confirmed project.
- **Workspace** — if the user asks for "the whole workspace", roll up across **all projects** in the workspace. No project pick is needed in that case.

Stop at that level — **do not** descend to a single test, and resolve only the levels the chosen scope needs.

### Step 0c — AI Consent gate

Check the resolved **account's** AI-consent state via `blazemeter_account read`. If the account has **not** consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` — before invoking the engine or fetching anything. (The consent gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

### Step 0d — Census the window with `plan` (the practicality checkpoint)

Do **not** enumerate the test catalog — activity is what costs, so the census is **window-first**: one server-side-filtered listing reports how many runs (across how many tests) fall in the window. Resolve the window (Step 1) first, then:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py plan --project-id <id> \
  --from <window start> --to <window end>       # ISO-8601 or epoch; defaults to the last 24h
# or:  --workspace-id <id>     (exactly one scope flag)
```

The engine reads the **same credentials the MCP uses** from the environment — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file). Never pass keys on the command line. If it exits with a credentials error, show the user which variables to set and stop.

**Practicality guard:** show the census to the user. A quarter is a long window — the sweep's cost scales with `runs_in_window` (report fetches per run plus a baseline lookup per active test), so hundreds of runs is worth a heads-up and an offer to **narrow to a specific project or shorten the window** before proceeding. Never silently truncate the scope.

### Step 0e — Display the resolved scope and the census, then continue

Display the cross-test context block before acting, so the run is auditable:

```
Scope:      Project <project name>  (ID: <project_id>)        ← or "Workspace <name> (ID)" for a workspace report
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
Window:     <resolved window, e.g. Q2 2026: 2026-04-01 → 2026-06-30>
Activity:   <N> runs across <M> tests in the window           ← from the plan census
```

Carry this resolved scope forward as **conversational memory** for later skills in the same conversation (display it, allow a one-step "switch"); **never persist it** to disk.

## Step 1 — Resolve the window

Default to the **last quarter** (the most recent full calendar quarter, or the trailing ~90 days) ending now. Let the user override in natural language — "this quarter", "last 90 days", "since the 1.4 release", or an explicit date range. Compute a concrete `[from, to]` timestamp pair and **display it** (in Step 0e's block). The engine filters runs by overlap with this window.

## Step 2 — Run the sweep (one engine invocation, the sole data source)

One engine invocation does the whole bulk pull and all the deterministic judgment — listing each test's executions, keeping runs that overlap the window, fetching each kept run's reports, resolving each test's baseline, and computing the KPI deltas:

```bash
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py sweep \
  --project-id <id> \                           # or --workspace-id (exactly one)
  --from 2026-04-01T00:00:00Z --to 2026-07-01T00:00:00Z \
  --baseline-file .blazemeter/baseline.json \   # only if the user's repo has one
  --pins <scratch>/pins.json \                  # only if the user pinned baselines this conversation
  --out <scratch>/portfolio.json
```

- **`--baseline-file`** — pass the user's committed `.blazemeter/baseline.json` when the repo has one; its entries (a flat `{test_id: execution_id}` map) pin those tests' baselines.
- **`--pins`** — if the user pinned a baseline for specific tests earlier **in this conversation**, write those as a small JSON map `{"<test_id>": "<execution_id>"}` to a scratch file and pass it. Pins outrank the committed file. Omit otherwise.
- Baseline precedence per test is applied inside the engine: **conversational pin → committed file → last passing run** (from the test's own history, which may legitimately predate the window). A test with no passing run gets `"source": "none"` — no baseline is invented.
- Stdout is a **five-line summary** (tests swept, failures/regressions, fetch coverage, output path) — show it to the user as progress. The full result is the JSON at `--out`, and that file is the **sole data source** for every number in the report.
- **Exit codes:** `0` success; `2` usage/credentials (tell the user what to set/fix); `3` scope-level failure **or** too many fetch failures (default threshold 20%, tune with `--max-failure-rate`). On `3` the JSON may still exist — its `coverage` block says exactly what's missing; label the report **partial**, never complete.

## Step 3 — Read the sweep JSON and derive each test's scorecard row

Read `--out`. It is compact — one entry per test that ran (idle tests are only counted). Everything numeric is already computed; **do not recompute or second-guess the arithmetic**. Per test you get: run counts (`runs_in_window`, `kpi_runs`, `passed`/`failed`, `skipped_partial`, `inconclusive`, `still_running`), `baseline` (`source`: `pin | file | last-passing | none`, and the execution id), `candidate_execution_id`, `deltas` vs baseline (avg/p95/p99/throughput/error-rate, each with a `pct` and an `adverse` flag; throughput judged per-virtual-user when the load config changed, flagged `normalized_per_vu`), `worst_kpi_move`, `regressed`, `notes`, `anomaly_status`, and `incident_candidates`.

Derive the portfolio columns from those fields — simple arithmetic on already-computed numbers, nothing re-fetched:

- **SLA compliance %** — `passed / kpi_runs * 100` (i.e. `passed / (passed + failed)`). When `kpi_runs` is 0 (only partial/inconclusive runs in the window), there is no compliance number — render it as `—` with a note, never `0%`.
- **Trend arrow** — from `deltas` (candidate vs the test's own baseline): any `adverse: true` delta → `degrading`; no adverse move but a primary KPI (p95, error rate, throughput) improved by ≥10% in the good direction → `improving`; otherwise `stable`. No baseline or no deltas → no arrow (`—`); don't invent a direction.
- **Regression flag** — `regressed` and `worst_kpi_move` verbatim; format the move as a short string, e.g. `p95 +34%` (or `error rate +2.1 pts` when the delta carries `points` because the baseline was clean).
- **Baseline source** — map `pin → pinned`, `file → committed file`, `last-passing → last-passing`, `none → no baseline` for display.
- **Health** — `critical` if the test had any failing run in the window (`failed > 0`) or its SLA compliance is below ~60%; `at-risk` if `regressed` while still green, or compliance is below ~90%; else `healthy`.
- **Incidents** — rank the union of all tests' `incident_candidates` by severity: outright **failures** first, then **large regressions** vs baseline (bigger move = higher), then **error spikes** (`error_spike` past 1%, severe past 5%; `endpoint_error_spike` near-100% on real traffic), then **anomalies** (weight a KPI/label recurring across multiple tests as systemic; a lone one-off is likely noise). Name the test, run id, metric, and baseline-vs-now numbers so each is actionable.

Treat `statistics_unavailable` as **insufficient data, not a finding** (never an incident, never "clean"); `inconclusive` runs are inconclusive, not green. A test whose notes include `baseline_is_only_run` gets "baseline run, no prior to compare", not a 0% move.

## Step 4 — Assemble the portfolio Report data model (JSON)

Build a single JSON object with **`kind: "portfolio"`** matching the portfolio data model below (`meta` / `summary` / `tests` / `incidents`), filling every row from Step 3's derivations. **Supply `generated_at` yourself** (current time, ISO 8601) — the template never reads the clock, so the render is deterministic. Put **no credentials** anywhere in the model.

```json
{
  "kind": "portfolio",
  "meta": {
    "title": "<scope name> — Performance Portfolio",
    "subtitle": "<N> tests · <window label, e.g. Q2 2026>",
    "generated_at": "<ISO 8601 now>",
    "window_start": "<ISO date>", "window_end": "<ISO date>",
    "context": {
      "account":   { "name": "<account name>",   "id": "<account_id>" },
      "workspace": { "name": "<workspace name>", "id": "<workspace_id>" },
      "project":   { "name": "<project name>",   "id": "<project_id>" },
      "tests_count": 0
    }
  },
  "summary": {
    "verdict": "STABLE | REGRESSED | AT-RISK | CRITICAL",
    "headline": "<one-line portfolio takeaway>",
    "narrative": ["<2-4 short paragraphs of expert assessment>"]
  },
  "tests": [
    { "name": "<test name>", "id": "<test_id>", "health": "healthy | at-risk | critical",
      "runs": 0, "sla_compliance_pct": 0, "trend": "improving | stable | degrading",
      "regressed": false, "worst_kpi_move": "<e.g. p95 +34%>",
      "baseline_source": "pinned | committed file | last-passing | no baseline", "note": "<…>" }
  ],
  "incidents": [
    { "severity": "critical | warning | info", "test": "<test name>", "run_id": "<execution_id>",
      "kpi": "<metric>", "detail": "<baseline-vs-now numbers / why>" }
  ]
}
```

Per-row sourcing: `name`/`id` from the sweep entry's `test_name`/`test_id`; `runs` = `kpi_runs` (evaluable runs only — report `skipped_partial` in the coverage footer, don't fold it in); the remaining columns from Step 3. **Idle tests are never fetched** — the sweep is window-first, so `tests[]` contains only tests that ran (`runs_in_window`/`tests_ran` at the top level); the scorecard simply has no idle rows. Omit `incidents` (empty array) when there are none — the template renders a tidy "none" state. Your contribution is the `summary` block: verdict, headline, and a short expert narrative grounded in the sweep's numbers. Keep the JSON ready to inject in Step 5.

## Step 5 — Emit the branded Portfolio Report (template fill, no interpreter)

The Portfolio Report is the **same shipped HTML template** as `bzm-report`, rendering itself from the data model in the browser — there is **no Python step and no local interpreter**. New report types plug in at the **data-model seam**, not by forking the renderer: the model's `kind` selects the portfolio section group (scope context, executive summary, portfolio charts, the per-test scorecard, ranked incidents). Produce the file with three deterministic actions:

1. **Read the template** at `${CLAUDE_PLUGIN_ROOT}/skills/bzm-report/assets/report-template.html`. It bakes in the CSS, the approximated-BlazeMeter brand vars, and the vendored client-side JS that dispatches on the model's `kind` and builds the portfolio sections.
2. **Serialize your Step 4 data model to JSON** and **replace the single token `{{REPORT_DATA_JSON}}`** with it. The token sits inside `<script>window.REPORT_DATA = {{REPORT_DATA_JSON}};</script>`, so before substituting, **HTML-escape every `</` in the JSON to `<\/`** — that guarantees a string value (e.g. a test name like `Catalog </checkout>`) can never close the `<script>` tag early. Substitute the token literally; do not otherwise reformat the template.
3. **Write the result** as a `.html` file (default `./bzm-reports/`, filename a slug of the scope name + `generated_at`). Use the `Write` tool — no shell, no `python`.

The output is fully self-contained (offline, no CDN — safe to email): the same single token is the only thing that varies run-to-run, so layout and branding stay deterministic. **Branding lives in the template** (the CSS `:root` vars + inline logo SVG) — never hardcode brand values in the data model; re-branding is a template edit, not a model change.

## Step 6 — Handle the edge cases gracefully

- **Empty window (nothing ran):** `tests_ran: 0` → **do not** fabricate a scorecard or render an empty HTML shell. Confirm the scope and window, state plainly that **nothing ran in this window**, note how many tests are in scope, and offer to widen the window.
- **Partial coverage:** surface the sweep's `coverage` block honestly — skipped partial runs, failed fetches (with counts), anomaly stats unavailable — in both the report narrative and the summary you give the user. Never present a partial sweep as complete.
- **No baseline for a test:** its row reads `no baseline`; judge it on absolute pass/fail only.
- **Drill-ins stay interactive:** when the user asks about one incident ("what happened in run 9101?"), that is a *single-run* question — answer it with the MCP (`blazemeter_execution read`, `read_all_reports`, `read_anomalies_stats` for **that** execution id, or hand off to `bzm-triage-failure`). To describe a test's SLA rules in prose, `blazemeter_tests read` gives its `failure_criteria.meta.*` labels. Don't re-run the sweep for a drill-in.

## Output template

After rendering, tell the user:

```
## BlazeMeter Portfolio Report: <scope name>
**Window:** <start> – <end>   |   **Tests in scope:** N (<idle> idle, <skipped> partial runs skipped)
**Verdict:** <STABLE / REGRESSED / AT-RISK / CRITICAL>
**Report file:** <path to the HTML>

### Highlights
- <worst test: health + worst KPI move vs its baseline>
- <portfolio SLA compliance: e.g. 4/5 tests ≥ 95%>
- <top incident, with numbers>

### Coverage notes
- Fetch coverage: <ok>/<attempted> (<failed> failed)   ← only when failures > 0
- Tests with no baseline: <N> — judged on absolute pass/fail only

Open the HTML file to see the full branded scorecard (per-test health, SLA-compliance %, trend arrows, regression flags, portfolio charts, ranked incidents).
```

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_*` list/read calls per test and per execution burns enormous time and tokens at portfolio scale — a quarter across a suite is thousands of payloads — and is exactly what the engine exists for. MCP is for Step 0's interactive picks, the consent gate, and single-run drill-ins afterward — nothing in between.
- **Census the window, don't walk the catalog.** Step 0 resolves the scope and runs `plan` for the **window census** — no paging `blazemeter_tests list`, and idle tests are never touched. A big census is a reason to *offer narrowing* to a project (or a shorter window), never to silently truncate.
- **Consent before sweep.** The AI-consent check (Step 0c) must pass before any `plan`/`sweep` invocation — the gate lives in the MCP layer, and the engine assumes it already happened.
- **Credentials are environment-only.** The engine reads `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (a key-file path) — the same variables the MCP uses. Never put a key on the command line, in the data model, or in the generated HTML (it's meant to be emailed — keep it secret-free).
- **Trust the engine's arithmetic.** Deltas, normalization, baseline choice, and status buckets are computed deterministically. Your derivations on top (SLA %, trend arrow, health) are simple mappings of those numbers — if a number looks wrong, say so and show it; don't silently recompute or re-fetch.
- **Deep windows can hit the history cap.** For each test the engine pages executions newest-first (50 per page, up to 20 pages ≈ 1,000 runs) and stops once a page predates the window. A test that ran more than ~1,000 times since the window started can have its **oldest in-window runs truncated** — a real possibility with quarter-long windows on hyperactive CI tests. If a very active test's `runs_in_window` looks suspiciously flat, say the history may be capped and offer a shorter window for that portfolio.
- **The engine already excludes partial runs.** Aborted/errored runs are counted (`skipped_partial`) but their KPIs never fold into the scorecard; `runs` in a scorecard row means **evaluable** (`kpi_runs`) runs. Report the skipped count in the coverage footer — keep the report honest.
- **`statistics_unavailable` is not a finding.** It means anomaly stats couldn't be read — insufficient data, never "anomalies detected" and never a clean bill.
- **Don't compare a run to itself.** Last-passing resolution excludes the candidate, so a still-green regression is detectable whenever any prior pass exists. `baseline_is_only_run` in a test's notes means a *pinned* baseline points at the candidate itself — the row reads "baseline run, no prior to compare", not a 0% move.
- **No SLA number without evaluable runs.** `kpi_runs: 0` (only partial/inconclusive/running runs in the window) means SLA compliance is `—`, not `0%` — a test must never look breaching because its only runs were aborted.
- **Same template, new report type at the data-model seam.** Set `kind: "portfolio"` — that selects the scorecard/incidents/portfolio-charts section group in the one shipped template. Don't fork the renderer or hand-write report HTML. (Omit `kind` and you get the single-test layout.)
- **`generated_at` is supplied, not read.** The template never reads the clock (deterministic render). Provide the current timestamp; `meta.title` and `meta.generated_at` are required.
- **Escape `</` before substituting.** The model is injected into a `<script>` tag, so any `</` inside a string value (a test name, a narrative line) must become `<\/` first — otherwise it can close the tag early. This is the only transform the JSON needs.
- **Failure-criteria labels come from the test, not the execution.** At portfolio altitude, lead with pass/fail counts and deltas; reach into `blazemeter_tests read` (`failure_criteria.meta.*` labels — never raw kpi ids or op codes) only when explaining *why* a specific run failed during a drill-in.
- **Companion to the digest.** `bzm-daily-digest` produces the same cross-test rollup as **markdown/terminal**; this skill is its **shareable HTML** form. Use the digest for a standup; use this for a stakeholder-facing scorecard.
- **Never persist scope.** The resolved account/workspace/project is conversational memory only. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing. Scratch files (`pins.json`, `portfolio.json`) go in the session scratch directory, not the user's repo; only the final `.html` lands where the user asked.
