---
name: bzm-ci-setup
description: Scaffold a ready-to-commit GitHub Actions workflow that runs a BlazeMeter test in CI and gates the job on the result — choose a gate policy (the test's own pass/fail failure criteria, or compare-vs-baseline against .blazemeter/baseline.json) and trigger(s) (on PR, on push to a branch, and/or on a schedule), with the workflow reading credentials only from ${{ secrets.BLAZEMETER_API_KEY }}. Use when asked to set up CI for a BlazeMeter test, add a performance gate to a repo, run a load test on PR/push/nightly, or generate a GitHub Actions workflow for BlazeMeter.
---

Scaffold a **GitHub Actions workflow** that runs a BlazeMeter Performance test on the trigger(s) you choose and **gates the job** on the outcome — then hand the user a ready-to-commit workflow file, the exact repo-secret setup steps, and a short README snippet. The skill resolves the test's context via the BlazeMeter MCP, lets the user pick the **gate policy** and **trigger(s)**, and generates the workflow with a deterministic script. The generated YAML authenticates to BlazeMeter **only** through `${{ secrets.BLAZEMETER_API_KEY }}` — it never embeds, logs, or echoes a token (ADR-0016, conventions §5/§6).

The deterministic YAML generation lives in a shared script — call it to produce the workflow rather than hand-writing YAML, so the output is consistent and fixture-tested:

```
${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_ci_scaffold.py
```

It takes the resolved `test_id`, the chosen trigger(s) and gate policy, and optional workflow name / push branch / cron, and prints the workflow YAML to stdout. Run `python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_ci_scaffold.py --help` for the interface. The script makes **no network calls** — the live MCP reads (resolving context) stay here in the prose.

## Step 0 — Resolve and confirm context (account → workspace → project → test)

This is the canonical Context Resolution step from `shared/conventions.md` §4. A CI gate is meaningless — and dangerous — without knowing exactly which test it runs, so always resolve and **display** the full context (with ids) before generating anything. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. Committing a workflow that runs the *wrong* test burns minutes on every push.

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

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than generating a workflow.

Present the resolved context to the user before continuing:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

If any link in the chain fails (e.g. a `project_id` is missing from the test response), **stop and report the gap** — do not scaffold CI against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

## Step 1 — Choose the gate policy

Ask which verdict should make the CI job **pass or fail**. Two policies are supported:

- **`pass-fail` (the test's own failure criteria)** — the job passes/fails on the execution's pass/fail verdict, i.e. the test's defined `failure_criteria`. Pick this when the test already has meaningful failure criteria (p95 < X ms, error rate < Y%, …). If `blazemeter_tests read` shows the test has **no** failure criteria, warn the user that this gate would be indeterminate (a run with no criteria is not a pass) and suggest either defining criteria or using compare-vs-baseline instead.
- **`compare-baseline` (compare vs the committed baseline)** — the job compares this run against the baseline recorded in `.blazemeter/baseline.json` and fails on a regression (ADR-0017). Pick this to catch *drift* even when the test has no hard criteria. This requires a committed baseline file (Step 4).

## Step 2 — Choose the trigger(s)

Ask **when** the gate should run. Any combination of these is allowed (the workflow always also adds `workflow_dispatch` so it can be run on demand from the Actions tab):

- **`pr`** — on every pull request (`pull_request`).
- **`push`** — on push to a branch (`push` → `branches: [<branch>]`); ask which branch (default `main`).
- **`schedule`** — on a cron schedule (`schedule` → `cron`); ask for the cadence (default `0 6 * * 1`, i.e. 06:00 UTC every Monday).

Confirm the choices back in plain language before generating (e.g. "Run on PRs and nightly at 02:00 UTC, gating on the test's failure criteria").

## Step 3 — Generate the workflow

Call the scaffold script with the resolved `test_id`, the chosen gate and trigger(s), and any optional overrides. Repeat `--trigger` once per trigger:

```
python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_ci_scaffold.py \
  --test-id <test_id> \
  --trigger pr --trigger schedule \
  --gate pass-fail \
  --branch main \
  --cron "0 2 * * *" \
  --name "BlazeMeter performance gate"
```

Write the printed YAML to **`.github/workflows/<file>.yml`** in the *user's* repo (suggest `blazemeter-performance.yml`; use a distinct name if combining several gated tests). Show the user the file path and the full contents.

The generated workflow:
- reads the BlazeMeter credential **only** from `${{ secrets.BLAZEMETER_API_KEY }}`, materializes it into a temp key file with mode `600` (without ever echoing it), and runs the test via the BlazeMeter REST API v4 — see the Gotcha on why CI uses REST rather than the MCP;
- for `pass-fail`, fails the job when the execution's verdict is fail (and flags an indeterminate run with no criteria);
- for `compare-baseline`, checks out the repo, reads `.blazemeter/baseline.json`, and fails the job on a regression versus the baseline execution.

## Step 4 — Repo-secret setup, and the baseline file (compare-baseline only)

Give the user the **exact** steps to provision the secret and (if needed) the baseline, since the committed workflow does not run until the secret exists:

**Always — add the repository secret** (the only credential the workflow uses):
1. In the repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**.
2. Name it exactly **`BLAZEMETER_API_KEY`**.
3. Paste the contents of the BlazeMeter API-key JSON (`{"id": "...", "secret": "..."}`) as the value. This is the same key the BlazeMeter MCP uses (conventions §6). **Never** commit this value or paste it into the workflow file — the workflow reads it by reference at run time.

If the project touches GitHub through an MCP-backed flow (e.g. opening the PR that adds this workflow, or posting status), use the **GitHub MCP** first per conventions §5 — `gh`/REST is only a documented fallback. Setting a repository *secret* is done by the user in the GitHub UI (or `gh secret set`), not by this skill: the skill never handles the token.

**Only for `compare-baseline` — commit the baseline file:**
1. Create / update `.blazemeter/baseline.json` (a `test_id → execution_id` map) with the **`bzm-baseline`** skill ("promote this run to the baseline"). It writes the file and shows a diff.
2. **Commit** `.blazemeter/baseline.json` alongside the workflow. The gate reads it on every run; without an entry for this `test_id`, the job fails fast with a clear message telling the user to create it.

## Output template

```
## BlazeMeter CI gate scaffolded: <test name> (test ID: <test_id>)
Account / Workspace / Project: <names + ids> (from Step 0)

### Configuration
- Gate policy: pass-fail (test failure criteria) | compare-baseline (.blazemeter/baseline.json)
- Triggers:    <PR | push to <branch> | schedule <cron>>(+ manual workflow_dispatch)

### File to commit
`.github/workflows/<file>.yml`
```yaml
<full generated workflow YAML — credentials only via ${{ secrets.BLAZEMETER_API_KEY }}>
```

### Required setup (workflow won't run until done)
1. Add repository secret BLAZEMETER_API_KEY (Settings → Secrets and variables → Actions)
   — value = the BlazeMeter API-key JSON; never commit it.
2. (compare-baseline only) Create & commit .blazemeter/baseline.json via the
   bzm-baseline skill.

### README snippet (optional — paste into the repo's README)
> Performance is gated in CI by BlazeMeter (`.github/workflows/<file>.yml`). It runs
> test <test_id> on <triggers> and fails the build on <a failure-criteria violation |
> a regression vs the committed baseline>. Add a `BLAZEMETER_API_KEY` repository secret
> to enable it.

### Notes
- <e.g. test has no failure criteria → pass-fail would be indeterminate, suggested
   compare-baseline; reminder to commit the baseline file; secret not yet set>
```

## Gotchas

- **Secrets-only, always (ADR-0016).** The generated YAML reads the credential **only** from `${{ secrets.BLAZEMETER_API_KEY }}` and contains no literal key, no key-file path with a value, and no `echo`/`cat` of the secret. Never paste a token into the workflow or into chat. The secret is provisioned once by the user in the GitHub UI; the workflow consumes it by reference. (The scaffold script enforces this; its tests assert no literal credential appears.)
- **The workflow won't run until the secret exists.** A committed workflow that references `secrets.BLAZEMETER_API_KEY` fails (or skips auth) until the user adds that repository secret. This friction is deliberate — it's the only way to keep a committed artifact credential-free. Tell the user to add the secret as the first step.
- **CI uses REST v4, not the MCP — and the prose says so (conventions §5).** The BlazeMeter MCP is an interactive server, not a headless CI runner, so the generated job drives the test via the documented REST API v4 fallback (start → poll the master → read the verdict/KPIs). That's the justified gap; the skill's *own* context resolution still uses the MCP first.
- **`pass-fail` needs real failure criteria.** A test with no `failure_criteria` yields an `unset`/indeterminate verdict, which is **not** a pass. If Step 0's `blazemeter_tests read` shows no criteria, warn the user and steer them to define criteria or use `compare-baseline`.
- **`compare-baseline` needs a committed baseline file.** The gate reads `.blazemeter/baseline.json` (keyed by `test_id`); if it's missing or has no entry for this test, the job fails fast by design. Create it with the `bzm-baseline` skill and commit it. The baseline file is the *user's* version-controlled repo state, not plugin-cached context (ADR-0017) — it's fine to commit.
- **Every gated run costs minutes/credits.** A PR or push trigger starts a real BlazeMeter execution on each event. Pick triggers deliberately — `schedule` (nightly) plus manual `workflow_dispatch` is often cheaper than gating every PR; confirm the cadence with the user.
- **One workflow per gated test.** The scaffold generates a single job for one `test_id`. To gate several tests, generate separate workflow files (distinct `--name` and filenames) rather than hand-merging jobs.
- **Don't write into THIS plugin's repo.** The workflow is for the *user's* repo at `.github/workflows/`; generate it as text and have the user commit it there. Never add it under the plugin's own `.github/workflows/`.
