---
name: bzm-baseline
description: Establish, update, resolve, and show the golden performance baseline for a BlazeMeter test — pin a specific execution as the baseline, resolve the active baseline (pinned id, else the last passing run), and read/write the committed .blazemeter/baseline.json used to gate CI. Use when asked to set, promote, pin, show, or look up the baseline a test's runs are compared against, or to update the baseline file in a repo.
---

Manage the **golden baseline** a BlazeMeter test's runs are compared against. This skill does four things: **pin** a specific execution as the baseline for a conversation, **resolve** the active baseline (the pinned id if present, otherwise the test's last passing run), maintain the committed **CI baseline file** `.blazemeter/baseline.json` (always showing the diff before writing), and **show** which execution is the current baseline and why, with its key KPIs so the user can sanity-check it.

There are **two baseline representations** (ADR-0017), and this skill spans both:
- **Interactive / conversational** — a pin is an explicit `execution_id` the user names for *this conversation* ("baseline against execution 98765"). Absent a pin, the baseline defaults to **the last passing run**, looked up live at call time. A pin is **conversational memory only — never persisted across sessions** (conventions §4.6).
- **CI gate** — a **file the user commits**, `.blazemeter/baseline.json`, a flat map of `test_id → execution_id`. The gate skill *reads* it; this skill *writes/updates* it, producing a diff the user reviews and commits like any other change. This is the user's own version-controlled repo state — not the plugin caching account context — so it does not violate the no-stored-context rule (ADR-0017).

The deterministic file and selection logic lives in a shared script — call it for those steps rather than hand-rolling JSON or sort order:

```
${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py
```

It exposes `resolve` (pinned-else-last-passing), `last-passing` (pick the most recent passing execution from a list), and `set` (pin `test_id → execution_id` in the file, printing a diff; writes only with `--write`). Run `python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py --help` for the interface. The **live MCP reads** (resolving context, listing executions, reading KPIs) stay here in the prose; the script never makes network calls.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

This is the canonical Context Resolution step from `shared/conventions.md` §4. A baseline is meaningless without knowing exactly which test it belongs to, so always resolve and **display** the full context (with ids) before pinning, resolving, showing, or writing anything. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. Writing the wrong `test_id → execution_id` into a committed CI file is worse than doing nothing.

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

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than proceeding.

Present the resolved context to the user before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a `project_id` is missing from the test response), **stop and report the gap** — do not write or resolve a baseline against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Decide the mode

Pick the action from the request (you may chain them — e.g. resolve, show, then write):

- **Pin** — record an explicit `execution_id` as the baseline for this conversation. Validate the id (Step 2) and remember it conversationally; do **not** write a file unless the user also asks to update CI.
- **Resolve** — return the active baseline for the test: the conversational pin if one is set, else the committed CI file's entry for this `test_id`, else the last passing run (Step 3).
- **Show** — resolve, then display the baseline execution with its key KPIs and *why* it is the baseline (Step 4).
- **CI write** — pin `test_id → execution_id` in `.blazemeter/baseline.json`, **always showing the diff before writing** (Step 5).

## Step 2 — Validate a candidate execution before it becomes a baseline

Whether pinning interactively or writing to CI, confirm the execution is a sound baseline first. Read it:

```
blazemeter_execution read  { execution_id: <id> }
   → captures: execution_status, ended, project_id, execution_name
```

- **Completion gate:** `ended` must be **NOT null** — a still-running execution has partial KPIs and must never be a baseline.
- **Pass gate:** a baseline should be a *passing* run. If `execution_status` is not a clean pass (it can be `unset`, `abort`, `error`, `noData`), warn and ask the user to confirm before pinning a non-passing run as the bar. (The script's last-passing selection enforces the same rule automatically.)
- **Scope check:** the execution's `project_id` should match the test's `project_id` from Step 0. If it doesn't, stop and report the mismatch — you may be about to baseline a run from a different test.

## Step 3 — Resolve the active baseline

Resolution order is **pin → committed CI file → last passing run** (ADR-0017):

1. **Conversational pin** — if the user pinned an `execution_id` earlier in this conversation, that is the baseline. Done.
2. **Committed CI file** — if a `.blazemeter/baseline.json` exists in the user's repo, read its entry for this `test_id`. Use the script (it parses, normalizes ids to strings, and surfaces a malformed file as an error rather than guessing):

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py resolve \
     --file .blazemeter/baseline.json --test-id <test_id>
   ```

   It prints `{"source": "pinned", "execution_id": "<id>"}` when the file has an entry. ("pinned" here means "pinned in the committed file"; a malformed file exits non-zero — report that, don't fall through silently.)
3. **Last passing run** — with no pin and no file entry, default to the most recent passing execution. List the test's executions and let the script choose:

   ```
   blazemeter_execution list  { test_id: <id>, limit: 50, offset: 0 }
   ```

   Page with `offset` in steps of 50 until you have enough history (the most recent passing run is usually on the first page; page further only if the first page has no pass). For each execution capture `id`, `status` (`execution_status`), and `end_time`. Hand that list to the script — it picks the **most recent passing** run (`PASSING_STATUSES`), with `end_time` as the ordering key and `id` as a deterministic tie-breaker:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py last-passing \
     --executions <executions.json>
   ```

   If it returns `null`, there is **no passing run** to baseline against — say so plainly and stop (don't silently pick a failed run).

## Step 4 — Show the baseline (resolve, then explain with KPIs)

After resolving (Step 3), pull the baseline execution's KPIs so the user can sanity-check it:

```
blazemeter_execution read_all_reports  { execution_id: <baseline_id> }
```

Use the **summary** sub-report for the headline KPIs (avg / p90 / p95 / p99 response time, throughput RPS, error rate, achieved peak concurrency). Present *which* execution is the baseline, **why** (pinned vs. committed-file vs. last-passing), and its KPIs (Output template below).

## Step 5 — Write / update the CI baseline file (always diff first)

To set the committed baseline, use the script's `set` action. It merges into any existing file (one file gates many tests — other entries are preserved), serializes deterministically (sorted keys) so diffs stay minimal, and **prints a unified diff**. It is a **dry run by default**; pass `--write` only after the user has seen and approved the diff:

```
# 1. Show the change (dry run — no file written)
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py set \
  --file .blazemeter/baseline.json --test-id <test_id> --execution-id <execution_id>

# 2. After the user approves the diff, apply it
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py set \
  --file .blazemeter/baseline.json --test-id <test_id> --execution-id <execution_id> --write
```

Then tell the user to **review and commit** `.blazemeter/baseline.json` like any other change — the file is theirs, version-controlled, and what the CI gate will read on the next push. Do not commit it for them.

> **GitHub note:** this skill writes a local file the user commits; it does not push or open PRs. If a workflow needs to *post* a baseline change (PR comment, commit status), that is GitHub-MCP-first per conventions §5 — but this skill stops at the local file.

## Output template

```
## BlazeMeter Baseline: <test name> (test ID: <test_id>)
Account / Workspace / Project: <names + ids> (from Step 0)

### Active baseline
- Execution: <execution_id> — <execution_name> (<ended date>)
- Source:    pinned (this conversation) | committed file (.blazemeter/baseline.json) | last passing run
- Status:    <execution_status>   Completed: <ended>

### Baseline KPIs
| KPI | Value |
|-----|-------|
| Avg RT (ms)        |   |
| p90 / p95 / p99 (ms)|   |
| Throughput (RPS)   |   |
| Error rate (%)     |   |
| Peak concurrency   |   |

### CI file change   ← only when writing .blazemeter/baseline.json
<unified diff from the script>
→ Review and commit .blazemeter/baseline.json to make this the CI baseline.

### Notes
- <e.g. "no passing run found — nothing to baseline", non-passing pin confirmed by user,
   pin is conversational only and won't persist, malformed baseline file, project mismatch>
```

## Gotchas

- **Two representations, kept separate (ADR-0017).** A conversational pin and the committed CI file are different things and can diverge. A pin lives only for the conversation and is **never** written to disk; only an explicit "update the baseline file" touches `.blazemeter/baseline.json`. Don't conflate them.
- **Never persist conversational context.** The committed baseline file is *the user's* repo state (test_id → execution_id) — fine to write when asked. Resolved **account/workspace/project** context is **not**: never cache it to disk (conventions §4.6, ADR-0012).
- **Always diff before writing.** Show the unified diff (the `set` action prints it on a dry run) and get approval before `--write`. The skill never commits the file for the user.
- **"Passing" is an explicit pass.** Last-passing selection counts only a clean pass verdict; `unset` (no criteria), `abort`, `error`, `noData`, and still-running runs are excluded — same posture as bzm-compare-runs. If `last-passing` returns null, there is no baseline to pick; say so rather than baselining a failed run.
- **Completion before baselining.** Confirm `ended != null` on any candidate — a partial run's KPIs look artificially good or bad and would poison every future comparison.
- **Malformed / missing baseline file.** A *missing* `.blazemeter/baseline.json` is an empty baseline (not an error). A *present but malformed* file is a real error — the script exits non-zero; report it and ask the user to fix the file, don't silently fall through to last-passing.
- **One file, many tests.** `.blazemeter/baseline.json` is keyed by `test_id`, so a write must **merge** (preserve other tests' entries) — the script does this; never overwrite the file with a single entry.
- **Pagination.** `blazemeter_execution list` maxes at 50 per call; page by `offset` only if the first page contains no passing run.
