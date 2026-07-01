---
name: bzm-portfolio-report
description: Generate ONE branded, self-contained HTML scorecard across MANY BlazeMeter tests in a workspace or project over a window (default a quarter) — per-test health, SLA-compliance %, trend arrows, and regression-vs-own-baseline flags, plus ranked incidents. Use when asked for a shareable/stakeholder portfolio report, a quarterly or release scorecard across a whole suite, an executive cross-test rollup, or a "how is the whole portfolio doing?" HTML you can email.
---

Produce the **Portfolio Report**: a single branded, self-contained HTML scorecard that rolls up **every test in a scope** (a workspace or project) over a window (default a quarter) — each test's health, SLA-compliance %, trend, and whether it regressed against **its own baseline**, plus a ranked cross-test incident list. It is the **shareable HTML rendering** of the same cross-test view `bzm-daily-digest` produces in markdown: reach for the digest when you want a scannable standup artifact in the terminal, and for this skill when you want a stakeholder-facing, emailable HTML scorecard. Where `bzm-report` trends **one** test across its runs, this skill is its **portfolio sibling** — the same engine, the same brand, one row per test instead of one row per run.

This skill **retrieves and normalizes** cross-test data, then fills the same shipped HTML template `bzm-report` uses (`skills/bzm-report/assets/report-template.html`). New report types are added at the **same data-model seam**, not by forking the renderer: you build a **portfolio** Report data model (JSON with `kind: "portfolio"`) and drop it into the single `{{REPORT_DATA_JSON}}` token; the template's baked-in CSS, vendored client-side charts, and approximated-BlazeMeter branding own the layout. The model's `kind` selects the portfolio section group (scorecard, incidents, portfolio charts). No local interpreter is involved: the render is a token replacement plus a file write, so it runs identically across the CLI, VS Code, and the desktop app.

## Step 0 — Resolve and confirm the *scope* (account → workspace → project), then enumerate its tests

This is the **cross-test** variant of Context Resolution. A portfolio report operates over **many tests at once**, so Step 0 resolves down to a **scope** (a workspace, or a project within it) and then **enumerates the tests in that scope** — it does **not** narrow to a single test. Every don't-assume guarantee of single-test resolution still applies; only the final level changes. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Resolve account → workspace → project (same tiered pick rule at each level)

Apply the uniform tiered pick rule at **each** level — account, then workspace, then project:

- Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → **display** it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
- To enumerate options, list one page (`blazemeter_account list` / `blazemeter_workspaces list` / `blazemeter_project list`, `limit: 50`).
  - **Fits a choice list** (small set — the first page is *not* full) → present an **interactive choice list**, every entry showing name + id (default marked), the user clicks one; if there are more options than the choice widget holds, fall back to a **numbered text list** with ids (e.g. `1. Acme (account 12345)`).
  - **Too big / paginated** (the first page comes back full → more pages exist, e.g. >50) → **don't dump it**; ask the user to **name, paste an id, or filter**. A pasted **id short-circuits** any level via a direct `read`; a **name** you resolve by paging and matching.
- Always show the **id** next to each name so same-named entities are distinguishable.
- **Name doesn't resolve cleanly:** no match → say so, show what *is* available, stop; multiple matches → list each candidate with its **parent and id** and let the user pick; 403 → report the access gap, don't retry. **Never fall back to the default** at any level.

### Step 0b — Choose the scope to roll up over

The portfolio rolls up over **one scope**:

- **Project** (default) — roll up the tests in the confirmed project.
- **Workspace** — if the user asks for "the whole workspace", roll up across **all projects** in the workspace (enumerate projects via `blazemeter_project list`, then enumerate each project's tests).

Stop at that level — **do not** descend to a single test.

### Step 0c — AI Consent gate

Check the resolved **account's** AI-consent state via `blazemeter_account read`. If the account has **not** consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` — before enumerating or fetching anything.

### Step 0d — Enumerate the tests in scope

Page `blazemeter_tests list { project_id: <id>, limit: 50, offset: 0 }` (stepping `offset` by 50) **to completion** — enumeration is the point here, so a full first page is **not** a reason to ask the user to name one test; keep paging and operate over the whole set. For a workspace-scope report, do this for each project. Capture each test's `test_id`, name, and its `failure_criteria` (`failure_criteria.rules[]` + `failure_criteria.meta.*` labels — keep these for SLA compliance).

If the scope is **so large that enumerating is impractical** (e.g. hundreds of tests across a sprawling workspace), say so and ask the user to **narrow to a specific project** — never silently truncate to "the first page".

### Step 0e — Display the resolved scope and the test count, then continue

Display the cross-test context block before acting, so the run is auditable:

```
Scope:      Project <project name>  (ID: <project_id>)        ← or "Workspace <name>" for a workspace report
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
Window:     <resolved window, e.g. Q2 2026: 2026-04-01 → 2026-06-30>
Tests:      <N> tests in scope
```

Carry this resolved scope forward as **conversational memory** for later skills in the same conversation (display it, allow a one-step "switch"); **never persist it** to disk.

## Step 1 — Resolve the window

Default to the **last quarter** (the most recent full calendar quarter, or the trailing ~90 days) ending now. Let the user override in natural language — "this quarter", "last 90 days", "since the 1.4 release", or an explicit date range. Compute a concrete `[window_start, window_end]` pair and **display it** (in Step 0e's block). Everything downstream filters runs by `start_time`/`end_time` falling inside this window.

## Step 2 — For each test, list and select the runs in the window

For every test enumerated in Step 0d, list its executions and keep only those that **overlap the window** — these are **independent per test, so fetch in parallel**:

```
blazemeter_execution list  { test_id: <id>, limit: 50, offset: 0 }
```

- **Pagination:** `list` maxes at 50 per call; executions come back newest-first. Page by `offset` only until you pass the start of the window (once a page's runs are all older than `window_start`, stop paging that test). Page **further back than the window** when you need a baseline that predates it (Step 4).
- Keep **finished, evaluable** runs: `ended != null`. **Skip** `aborted` / `error` / `noData` / `TERMINATED` runs — they have incomplete data that distorts the scorecard; **count** them separately as "skipped (partial)" so the report is honest about coverage, but don't fold their KPIs in.
- A test with **no run in the window** is **idle** — it has no scorecard row of live data; note it in the coverage footer (Step 5) rather than inventing values.

## Step 3 — Retrieve each kept run's KPIs

For each kept run across all tests, fetch — **independent per run and across tests, so fan them out in parallel**:

```
blazemeter_execution read              { execution_id: <id> }   # execution_status + ended timestamp
blazemeter_execution read_all_reports  { execution_id: <id> }   # summary + errors + request_stats
```

Map the **summary** report's `overall_metrics` into KPI fields (the field names differ — map explicitly, same mapping `bzm-report` uses):

| Report field (`overall_metrics`) | KPI |
| --- | --- |
| `average_response_time_ms` | `avg_rt_ms` |
| `percentile_95_ms` | `p95_ms` |
| `percentile_99_ms` | `p99_ms` |
| `average_throughput_per_second` | `rps` |
| `error_rate_percent` | `error_rate_pct` |
| `max_concurrent_users` | `concurrency` |

Take each run's `status` from `blazemeter_execution read` → `execution_status`.

## Step 4 — Per test: SLA compliance, trend, and regression vs its OWN baseline

For **each test** in scope, compute the row the scorecard needs:

### 4a. SLA compliance %

Count the test's in-window runs whose `execution_status` is `pass` vs `fail`; `sla_compliance_pct = pass / (pass + fail) * 100`. Describe the rules (if the user drills in) from the **test's `failure_criteria.meta.*`** labels — **never raw kpi ids or op codes**.

### 4b. Trend

From the test's in-window run series (ordered oldest → newest), classify the primary KPI direction: `improving` (p95/error trending down), `degrading` (trending up), or `stable`. The series itself is the trend — no extra modeling.

### 4c. Regression vs the test's own baseline (reuse `bzm_baseline.py` — don't re-implement)

A run can pass its criteria yet be **meaningfully slower than the test's golden baseline** — exactly what a portfolio scorecard exists to surface. Resolve **each test's own baseline** and compare its most significant in-window run against it. **Reuse the shared script and concept from `bzm-baseline` — do not re-implement baseline logic.** Resolution order, per test:

1. **Conversational pin** — if the user pinned a baseline `execution_id` for this test earlier in the conversation, use it.
2. **Committed CI file** — if the repo has `.blazemeter/baseline.json`, read its entry for this `test_id`:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py resolve \
     --file .blazemeter/baseline.json --test-id <test_id>
   ```

   It prints `{"source": "pinned", "execution_id": "<id>"}` when present. A **malformed** file exits non-zero — surface that for the test, don't silently swallow it.
3. **Last-passing run** — with no pin and no file entry, default to the test's most recent passing run (it may predate the window — page history back as needed). Build a JSON list of the test's executions (`id`, `status`, `end_time`) and let the script choose:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py last-passing \
     --executions <executions.json>
   ```

   If it returns `null`, the test has **no passing run to baseline against** — set `baseline_source` to `"no baseline"`, leave `regressed` based on absolute pass/fail only, and don't invent a reference.

Read the resolved baseline's KPIs once per test (`blazemeter_execution read_all_reports { execution_id: <baseline_id> }`, the **summary** sub-report). A test is **`regressed: true`** if any tracked KPI (avg/p95/p99 RT, error rate — and RPS inverted, *lower* is worse) moved **≥ 10%** in the worse direction vs its baseline, **or** any in-window run failed its criteria. Record the **worst KPI move** (the single largest adverse % change, named — e.g. `p95 +34%`). **Normalize for load-config drift:** if a run's concurrency differs from the baseline, raw RPS isn't comparable — normalize to RPS-per-virtual-user before flagging a throughput regression, and note it. Don't compare a run to itself: if a test's only in-window run *is* its resolved baseline, mark it "baseline run, no prior to compare".

### 4d. Health

Derive each test's **health** from 4a–4c: `critical` (failed criteria in-window or breaching SLA on most runs), `at-risk` (regressed vs baseline but still green, or SLA compliance below a comfortable bar), else `healthy`.

## Step 5 — Assemble the portfolio Report data model (JSON)

Build a single JSON object with **`kind: "portfolio"`** matching the portfolio data model below (`meta` / `summary` / `tests` / `incidents`). **Supply `generated_at` yourself** (current time, ISO 8601) — the template never reads the clock, so the render is deterministic. Put **no credentials** anywhere in the model.

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

Leave `tests` rows for **idle** tests out of the array (note them in the coverage footer instead); omit `incidents` (empty array) when there are none — the template renders a tidy "none" state. Rank `incidents` by severity (outright failures → large regressions → error spikes), naming the test, run, metric, and baseline-vs-now numbers so each is actionable. Keep the JSON ready to inject in Step 6.

## Step 6 — Emit the branded Portfolio Report (template fill, no interpreter)

The Portfolio Report is the **same shipped HTML template** as `bzm-report`, rendering itself from the data model in the browser — there is **no Python step and no local interpreter**. Produce the file with three deterministic actions:

1. **Read the template** at `${CLAUDE_PLUGIN_ROOT}/skills/bzm-report/assets/report-template.html`. It bakes in the CSS, the approximated-BlazeMeter brand vars, and the vendored client-side JS that dispatches on the model's `kind` and builds the portfolio sections (scope context, executive summary, portfolio charts, the per-test scorecard, and ranked incidents).
2. **Serialize your Step 5 data model to JSON** and **replace the single token `{{REPORT_DATA_JSON}}`** with it. The token sits inside `<script>window.REPORT_DATA = {{REPORT_DATA_JSON}};</script>`, so before substituting, **HTML-escape every `</` in the JSON to `<\/`** — that guarantees a string value (e.g. a test name like `Catalog </checkout>`) can never close the `<script>` tag early. Substitute the token literally; do not otherwise reformat the template.
3. **Write the result** as a `.html` file (default `./bzm-reports/`, filename a slug of the scope name + `generated_at`). Use the `Write` tool — no shell, no `python`.

The output is fully self-contained (offline, no CDN — safe to email): the same single token is the only thing that varies run-to-run, so layout and branding stay deterministic. **Branding lives in the template** (the CSS `:root` vars + inline logo SVG) — never hardcode brand values in the data model; re-branding is a template edit, not a model change.

## Output template

After rendering, tell the user:

```
## BlazeMeter Portfolio Report: <scope name>
**Window:** <start> – <end>   |   **Tests in scope:** N (<idle> idle, <skipped> partial skipped)
**Verdict:** <STABLE / REGRESSED / AT-RISK / CRITICAL>
**Report file:** <path to the HTML>

### Highlights
- <worst test: health + worst KPI move vs its baseline>
- <portfolio SLA compliance: e.g. 4/5 tests ≥ 95%>
- <top incident, with numbers>

Open the HTML file to see the full branded scorecard (per-test health, SLA-compliance %, trend arrows, regression flags, portfolio charts, ranked incidents).
```

## Gotchas

- **Cross-test scope, not one test.** Step 0 resolves to a **scope** and **enumerates** its tests — a full first page of `blazemeter_tests list` means "keep paging", **not** "ask the user to name one test". Only an impractically large scope warrants asking the user to narrow to a project.
- **Same engine, new report type at the data-model seam.** Set `kind: "portfolio"` — that is what selects the scorecard/incidents/portfolio-charts section group in the one shipped template. Don't fork the renderer or hand-write report HTML; fill `bzm-report`'s `assets/report-template.html`. (Omit `kind` and you get the single-test layout.)
- **Per-test baseline via the shared script.** Resolve each test's baseline **per test** (pinned → committed `.blazemeter/baseline.json` → last-passing) via `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py` — don't re-implement it, and don't share one baseline across tests. `last-passing` → `null` means "no baseline": set `baseline_source: "no baseline"` and fall back to absolute pass/fail. A baseline may predate the window — page history further back. A malformed committed file exits non-zero — surface it.
- **Field-name mapping is exact.** `average_response_time_ms` / `average_throughput_per_second` / `error_rate_percent` / `percentile_9X_ms` / `max_concurrent_users` → `avg_rt_ms` / `rps` / `error_rate_pct` / `p9X_ms` / `concurrency`. A mis-key silently drops a KPI.
- **`generated_at` is supplied, not read.** The template never reads the clock (deterministic render). Provide the current timestamp; `meta.title` and `meta.generated_at` are required.
- **Completion = `ended != null`.** Skip `aborted` / `error` / `noData` / `TERMINATED` runs; count them as "skipped (partial)" in the footer, never fold their KPIs in. Idle tests (no in-window run) get no scorecard row — note them in coverage, don't invent values.
- **Load-config drift.** If concurrency varies vs the baseline, raw RPS isn't apples-to-apples — normalize to RPS-per-VU before flagging a throughput regression, and say you did.
- **Pagination.** Every `list` action maxes at 50 — page by `offset`. Per-test execution lists and per-execution reads are **independent — parallelize them**; a serial sweep over a whole workspace is needlessly slow.
- **Failure-criteria labels come from the test, not the execution.** Describe SLA rules with the test object's `failure_criteria.meta.*` labels; the execution only carries the overall `execution_status` (no per-criterion per-run result) — attribute a failing run by comparing its summary KPIs to the rule's threshold.
- **No credentials in the model or output.** The data model holds data + narrative only; Platform Credentials never belong in it (the template only ever sees what you inject). The generated HTML is shareable/emailable — keep it secret-free.
- **Escape `</` before substituting.** The model is injected into a `<script>` tag, so any `</` inside a string value (a test name, a narrative line) must become `<\/` first — otherwise it can close the tag early. This is the only transform the JSON needs.
- **Companion to the digest.** `bzm-daily-digest` produces the same cross-test rollup as **markdown/terminal**; this skill is its **shareable HTML** form. Use the digest for a standup; use this for a stakeholder-facing scorecard.
- **MCP-first.** Every retrieval is a `blazemeter_*` MCP action; no REST v4 fallback is needed. Only a genuine MCP gap would justify a documented REST call.
- **Never persist scope.** The resolved account/workspace/project is conversational memory only — never written to disk. The committed `.blazemeter/baseline.json` is the user's own repo state and a different thing.
