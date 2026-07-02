---
name: bzm-set-baseline
description: Set the committed golden baseline for a BlazeMeter test — pin a test_id → execution_id entry into the version-controlled .blazemeter/baseline.json (always showing a diff before writing) so CI gates and comparisons have a durable reference run. Use when asked to set, promote, commit, or update the baseline a test's runs are compared against in CI, or to write/update the baseline file in a repo.
---

Write the **committed golden baseline** for a BlazeMeter test: pin a `test_id → execution_id` entry into `.blazemeter/baseline.json` — a flat map **the user commits** to their repo — always showing the diff before writing. This file is what CI gates and baseline comparisons read as the durable reference run.

This skill only **writes** the file. Everything read-side — resolving which execution is the *active* baseline (conversational pin → committed file → last passing run), showing the baseline with its KPIs, or pinning a baseline for the current conversation only — lives in `bzm-test-analysis`; comparing runs against the baseline does too. The file is the user's own version-controlled repo state — not cached account context — so writing it (at the user's request, diff-first) does not violate the never-persist-context rule.

The deterministic file logic lives in a shared script — call it rather than hand-rolling JSON or sort order:

```
${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py
```

Its `set` action pins `test_id → execution_id` in the file, printing a diff; it writes only with `--write`. Run `python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py --help` for the interface. The **live MCP reads** (resolving context, validating the execution) stay here in the prose; the script never makes network calls.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

A baseline is meaningless without knowing exactly which test it belongs to, so always resolve and **display** the full context (with ids) before writing anything. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. Writing the wrong `test_id → execution_id` into a committed CI file is worse than doing nothing.

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

If any link in the chain fails (e.g. a `project_id` is missing from the test response), **stop and report the gap** — do not write a baseline against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Identify and validate the execution to baseline

The user usually names the execution ("promote run 98765 to the baseline"). If they didn't ("set the baseline to the latest good run"), resolve the candidate with `bzm-test-analysis` (its baseline-lookup picks the last passing run), confirm the pick with the user, then continue here.

Before it becomes the committed bar, confirm the execution is sound. Read it:

```
blazemeter_execution read  { execution_id: <id> }
   → captures: execution_status, ended, project_id, execution_name
```

- **Completion gate:** `ended` must be **NOT null** — a still-running execution has partial KPIs and must never be a baseline.
- **Pass gate:** a baseline should be a *passing* run. If `execution_status` is not a clean pass (it can be `unset`, `abort`, `error`, `noData`), warn and ask the user to confirm before committing a non-passing run as the bar.
- **Scope check:** the execution's `project_id` should match the test's `project_id` from Step 0. If it doesn't, stop and report the mismatch — you may be about to baseline a run from a different test.

Optionally show the run's headline KPIs (`blazemeter_execution read_all_reports`, summary sub-report: avg / p90 / p95 / p99 response time, throughput RPS, error rate, peak concurrency) so the user can sanity-check what they're about to enshrine.

## Step 2 — Write the file (always diff first)

Use the script's `set` action. It merges into any existing file (one file gates many tests — other entries are preserved), serializes deterministically (sorted keys) so diffs stay minimal, and **prints a unified diff**. It is a **dry run by default**; pass `--write` only after the user has seen and approved the diff:

```bash
# 1. Show the change (dry run — no file written)
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py set \
  --file .blazemeter/baseline.json --test-id <test_id> --execution-id <execution_id>

# 2. After the user approves the diff, apply it
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py set \
  --file .blazemeter/baseline.json --test-id <test_id> --execution-id <execution_id> --write
```

Then tell the user to **review and commit** `.blazemeter/baseline.json` like any other change — the file is theirs, version-controlled, and what the CI gate will read on the next push. Do not commit it for them.

> **GitHub note:** this skill writes a local file the user commits; it does not push or open PRs. If a workflow needs to *post* a baseline change (PR comment, commit status), do that with the GitHub MCP — but this skill stops at the local file.

## Output template

```
## BlazeMeter Baseline set: <test name> (test ID: <test_id>)
Account / Workspace / Project: <names + ids> (from Step 0)

### New baseline
- Execution: <execution_id> — <execution_name> (<ended date>)
- Status:    <execution_status>   Completed: <ended>

### File change
<unified diff from the script>
→ Review and commit .blazemeter/baseline.json to make this the CI baseline.

### Notes
- <e.g. non-passing run confirmed by user, project mismatch resolved, file created fresh>
```

## Gotchas

- **Always diff before writing.** Show the unified diff (the `set` action prints it on a dry run) and get approval before `--write`. The skill never commits the file for the user.
- **One file, many tests.** `.blazemeter/baseline.json` is keyed by `test_id`, so a write must **merge** (preserve other tests' entries) — the script does this; never overwrite the file with a single entry.
- **Completion before baselining.** Confirm `ended != null` on any candidate — a partial run's KPIs look artificially good or bad and would poison every future comparison.
- **"Passing" is an explicit pass.** `unset` (no criteria), `abort`, `error`, `noData`, and still-running runs are not a clean pass — warn and get explicit confirmation before committing one as the bar.
- **Malformed baseline file.** A *missing* file is fine (the script creates it). A *present but malformed* file is a real error — the script exits non-zero; report it and ask the user to fix the file rather than clobbering it.
- **Write-only by design.** Resolving/showing the *active* baseline, conversational pins, and comparisons live in `bzm-test-analysis` — route read-side questions there rather than answering them here.
- **Never persist conversational context.** The committed baseline file is *the user's* repo state — fine to write when asked. Resolved **account/workspace/project** context is **not**: never cache it to disk.
