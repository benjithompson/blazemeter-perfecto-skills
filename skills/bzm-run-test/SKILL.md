---
name: bzm-run-test
description: Run a BlazeMeter Performance test end-to-end — optionally set a simple load profile, start the execution, poll to completion, and report a pass/fail summary against the test's failure criteria. Use when asked to run, kick off, launch, or smoke/validate a BlazeMeter test and report whether it passed.
---

Run a BlazeMeter Performance test from start to finish and produce a concise pass/fail verdict: resolve the test's context, optionally configure a simple load profile (with explicit confirmation), start the execution, poll until it ends, and summarize the result against the test's failure criteria using readable labels.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

This is the canonical Context Resolution step from `shared/conventions.md` §4. Always resolve and **display** the full context (with ids) before starting anything, so the user can confirm you're operating on the right test. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. Running the wrong test costs real test minutes and load — get this right first.

### Step 0a — Identify the target test (two entry paths)

- **A `test_id` was given** → trust it and resolve *upward* (the chain in Step 0b). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a test *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, presented as a confirmable/overridable suggestion; if a level has exactly one option, just display it.
  - To enumerate, list one page (`limit: 50`). **Small set** (page not full) → numbered list, each entry with its id, user picks. **Too big to list** (page comes back full) → don't dump it; ask the user to name or paste the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
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

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding to configure or run.

Present the resolved context to the user before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a project_id is missing from the test response), **stop and report the gap** — do not start a run against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — (Optional) Configure a simple load profile — only with explicit confirmation

This is **optional** and only happens when the user asks to change the load *or* explicitly approves a suggested profile. `configure_load` **mutates the user's saved test configuration** — never call it silently or on your own initiative. If the user just wants to run the test as-is, **skip straight to Step 2** and run with whatever load is already configured.

If the user does want a simple profile, gather the knobs and confirm them back **before** the call. `hold-for` and `iterations` are **mutually exclusive** — per the MCP, *"don't use iterations if hold-for is provided"* — so pick **one** way to bound the run:

**Timed run (the usual choice)** — bound the run by duration with `hold-for`:

```
blazemeter_tests configure_load {
  test_id:     <id>,
  concurrency: <virtual users>,
  ramp-up:     <e.g. "1m">,
  hold-for:    <e.g. "1m">
}
```

**Iteration-capped run (alternative)** — instead bound the run by a fixed number of iterations per user, with **no** `hold-for`:

```
blazemeter_tests configure_load {
  test_id:     <id>,
  concurrency: <virtual users>,
  ramp-up:     <e.g. "1m">,
  iterations:  <iterations per user>
}
```

- **`concurrency`** — number of virtual users.
- **`ramp-up`** — how long to ramp from 0 to full concurrency. Specified in **minutes** (e.g. `"1m"`).
- **`hold-for`** — how long to *stay* at full concurrency. **This is what actually bounds a timed run's duration.** Specified in **minutes** (e.g. `"1m"`). Don't combine it with `iterations`.
- **`iterations`** — an alternative bound: a fixed iteration count per user instead of a held duration. Use it *only* when you are **not** setting `hold-for`.

**Quick / validation default (suggest, then confirm):** for a smoke or sanity run, propose minimal load — **concurrency 1, hold-for "1m"** (a short ramp-up like "1m" is fine). State the profile in plain language and get a yes before calling `configure_load`. Example:

```
Proposed load profile (a quick validation run):
  concurrency: 1 user
  ramp-up:     1m
  hold-for:    1m
This will overwrite the test's current load settings. Apply it? (yes / no / different numbers)
```

After `configure_load` returns, **echo back the load the test now has** so the user sees exactly what will run.

> **Bounding the duration — read the Gotcha below.** Use **`hold-for`** to cap how long the run lasts. Setting `iterations` *alone* does **not** shorten the run: a previously-saved large `holdFor` stays in place and the test keeps holding for that whole window. To get a short run, always set `hold-for` explicitly (e.g. `"1m"`).

## Step 2 — Start the execution

With context confirmed (and load set, if the user chose to), start the run. This **consumes test minutes / credits**, so only proceed once the user has confirmed the test and load.

```
blazemeter_execution start { test_id: <id> }
```

Capture from the response:
- `execution_id` (the master id — use it for every subsequent call)
- `execution_url` (the live BlazeMeter report link — surface this to the user immediately so they can watch in the UI)

## Step 3 — Poll to completion

Poll the execution and **wait for it to finish**. The reliable completion signal is the **`ended` field**, not the textual status:

```
blazemeter_execution read { execution_id: <id> }
```

- **`ended == null` ⇒ still running.** Keep polling. Use an unobtrusive cadence — roughly every 15–30s — and tell the user it's in progress (e.g. report elapsed time and the current `execution_status` if present). Don't busy-loop.
- **`ended != null` ⇒ finished.** Stop polling and move to Step 4.

Do **not** rely on a status string to detect completion — `ended` going non-null is the authoritative signal. If the run is taking far longer than the configured `hold-for` + `ramp-up` would imply, say so (it may indicate a stale large `holdFor` — see Gotchas) but keep polling until `ended` is set or the user asks to stop.

## Step 4 — Read results and report the verdict

Once `ended` is non-null, gather the outcome:

```
blazemeter_execution read          { execution_id: <id> }   # overall execution_status only
blazemeter_execution read_summary  { execution_id: <id> }   # aggregate KPIs (overall_metrics)
```

The overall pass/fail verdict is `execution_status` from `blazemeter_execution read`. Interpret it precisely:

| `execution_status` | Meaning |
|---|---|
| `pass` | All defined failure criteria were met. |
| `fail` | At least one failure criterion was violated (explain which — see below). |
| `unset` | **No failure criteria are defined ⇒ indeterminate, NOT a pass.** Say the run completed but there were no criteria to judge it against. |
| `abort` | The run was aborted before completing. |
| `error` | The run errored out. |
| `noData` | The run produced no data to evaluate. |

**The failure-criteria definitions and their readable labels come from the TEST object you already read in Step 0** (`blazemeter_tests read` returns `failure_criteria.rules[]` plus `failure_criteria.meta`), **not** from `blazemeter_execution read` (which only carries the overall `execution_status`). Render each criterion with its labels — `meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, and `meta.condition_labels` — e.g. "95th percentile response time > 2000 ms", never a raw kpi id and op code.

**There is no per-criterion per-run results array in the MCP.** `blazemeter_execution read` gives only the single overall `execution_status` — it does **not** return a pass/fail flag for each individual criterion. So when the overall status is `fail`, **explain the verdict by comparing this run's `read_summary` KPIs against the criteria thresholds** from the test object. For example: a criterion of "error rate % > 4" against a run whose `read_summary.overall_metrics.error_rate_percent` is 26.67% → that threshold was violated. Match each criterion's KPI to the corresponding `overall_metrics` field (e.g. `percentile_95_ms` for a p95 criterion, `error_rate_percent` for an error-rate criterion, `average_response_time_ms` for an avg-RT criterion) and show actual-vs-threshold. Present this as your reasoned interpretation, not as data the API returned per criterion.

From `read_summary` → `result[0].overall_metrics`, pull the headline KPIs: `average_response_time_ms`, `percentile_90_ms`, `percentile_95_ms`, `average_throughput_per_second` (RPS), `error_rate_percent`, and `max_concurrent_users` (achieved peak concurrency).

## Output template

```
## BlazeMeter Test Run: <test name> (ID: <test_id>)

**Result:** <PASS | FAIL | INDETERMINATE (no criteria) | ABORTED | ERROR | NO DATA>
**Execution:** <execution_id>   |   **Report:** <execution_url>
**Duration:** <started> → <ended>

### Load profile
- Configured: Concurrency: <N> users   |   Ramp-up: <…>   |   Hold-for: <…>   |   Iterations: <… or n/a>
  (source: the test's `override_executions[]` from `blazemeter_tests read` in Step 0 — `concurrency`, `holdFor`, `rampUp`, `iterations`; note "configured this run" if this skill set it via `configure_load`, otherwise "as previously configured")
- Achieved peak concurrency: <max_concurrent_users from read_summary.overall_metrics>

### Headline KPIs
| Avg RT | p90 | p95 | RPS | Error % | Peak users |
|--------|-----|-----|-----|---------|------------|
| …      | …   | …   | …   | …       | …          |

### Failure criteria
(definitions + labels from the test's `failure_criteria` in Step 0; overall verdict is `execution_status`. The per-criterion read below is *your* comparison of this run's KPIs against each threshold — the MCP returns no per-criterion result.)
- <criterion in readable labels> — <within threshold / VIOLATED> (actual KPI from read_summary vs threshold)
- …
  (if execution_status == unset: "No failure criteria defined — result is indeterminate, not a pass.")

### Verdict
1–2 sentences: did it pass, what (if anything) failed, and the obvious next step.
```

## Gotchas

- **`hold-for` bounds the run; `iterations` alone does not.** Setting only `iterations` leaves any previously-saved large `holdFor` in place, so the test keeps holding for that full window and the run runs long. To bound a timed run, set `hold-for` explicitly (e.g. `"1m"`). A quick validation default is concurrency 1, hold-for "1m".
- **`hold-for` and `iterations` are mutually exclusive.** Per the MCP, *"don't use iterations if hold-for is provided"* — never pass both in one `configure_load` call. Use `hold-for` for a timed run, or `iterations` (with no `hold-for`) for an iteration-capped run.
- **Load durations are minute-granular.** `ramp-up` and `hold-for` are specified in minutes (e.g. `"1m"`), not seconds.
- **`configure_load` mutates the saved test.** It overwrites the test's stored load settings — only call it with explicit user confirmation, and echo back the resulting profile.
- **Completion = `ended != null`, not a status string.** While running, `ended` is `null`; the textual `execution_status` can read intermediate or empty values mid-run. Only treat the run as finished when `ended` becomes non-null.
- **`execution_status: unset` is not a pass.** It means the test has **no failure criteria defined**, so there was nothing to judge against — report it as indeterminate and suggest defining criteria, rather than implying success.
- **Distinguish the non-pass statuses.** `abort` (stopped early), `error` (run errored), and `noData` (nothing to evaluate) are each different from a clean `fail` — name which one occurred so the user knows whether to re-run or investigate.
- **Failure criteria live on the TEST, not the execution.** The criteria definitions and their readable labels come from `blazemeter_tests read` (`failure_criteria.rules[]` + `failure_criteria.meta.*`), captured in Step 0. `blazemeter_execution read` returns only the single overall `execution_status` — no per-criterion results, no criteria definitions. Explain a `fail` by comparing the run's `read_summary` KPIs against the test's thresholds.
- **Use label fields for criteria.** Always render failure criteria via `meta.general_labels`, `meta.rule_field_labels`, `meta.kpi_labels`, and `meta.condition_labels` — never raw kpi ids or operator codes.
- **Starting a run costs minutes/credits.** `blazemeter_execution start` consumes the account's test resources — confirm the test (and load) before starting; don't start speculatively.
- **Surface `execution_url` early.** Give the user the live report link right after `start` so they can watch progress in the BlazeMeter UI while you poll.
