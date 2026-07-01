---
name: bzm-pr-gate
description: Gate a pull request on BlazeMeter performance — run the test, resolve the baseline, compare candidate vs baseline, then post a PR comment with the KPI-diff table and ship/no-ship verdict and set a commit status reflecting pass/fail. Use when asked to gate a PR on a load test, check a pull request for performance regressions, or post a BlazeMeter performance verdict back onto a PR.
---

A flagship **Journey** that gates one pull request on performance. It orchestrates three existing skills — **bzm-run-test**, **bzm-baseline**, and **bzm-compare-runs** — and closes the loop back onto GitHub: it runs the test for the PR, resolves the baseline, compares candidate vs baseline, then **posts a PR comment** with the KPI diff + ship/no-ship verdict and **sets a commit status** on the PR's head commit. This is an interactive companion to `bzm-ci-setup` (which scaffolds the headless CI workflow); here a human drives the gate from chat against a specific PR.

**This skill does not reinvent run / baseline / compare logic — it delegates to those skills' procedures and only adds the orchestration and the GitHub round-trip.** Where a step says "as in bzm-run-test" (etc.), follow that skill's prose exactly (its MCP calls, its gotchas, its output); do not duplicate or paraphrase its rules here.

Integration posture: BlazeMeter work is **BlazeMeter-MCP-first**; the GitHub round-trip is **GitHub-MCP-first**. Credentials are **never embedded, logged, or echoed** — BlazeMeter auth comes from the MCP's env vars and GitHub auth comes from the GitHub MCP. The skill never handles a token.

## Step 0 — Resolve and confirm BOTH targets (the BlazeMeter test AND the PR)

A PR gate has two targets: the **BlazeMeter test** to run, and the **pull request** to post back onto. Resolve and **display both** before doing anything that costs minutes or writes to GitHub.

### Step 0a — Resolve the BlazeMeter test (single-test Context Resolution)

The gate operates on **one** test for the PR. Resolve and **display** the full context (with ids) before running anything. **Don't assume:** the user may belong to multiple accounts, each with multiple workspaces/projects/tests, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice. Gating a PR against the wrong test posts a misleading verdict and burns minutes.

**Identify the target test (two entry paths):**

- **A `test_id` was given** → trust it and resolve *upward* (the chain below). The displayed context block stands as confirmation; no menu needed.
- **Nothing, or only a test *name*** → resolve *top-down* first. Establish the account, then workspace, then project, applying the uniform tiered pick rule at each level:
  - Start from the `blazemeter_user read` default, but **don't assume it's unambiguous — enumerate the level (next bullet) to see how many options exist**: exactly one → display it and proceed; more than one → present the numbered pick and **stop** for the user's choice (never silently take the default).
  - To enumerate, list one page (`limit: 50`), then present the options as a **choice list** by preference. **Fits the choice widget** (a handful) → interactive **choice list**, each entry showing its name + id (default marked), user clicks one. **More than the widget holds but still enumerable** → fall back to a **numbered text list** with ids, user picks a number or pastes an id. **Too big / paginated** (page comes back full → more pages, e.g. >50) → don't dump it; ask the user to **name, paste an id, or filter** the workspace/project/test (a pasted **id** short-circuits via direct `read`; a **name** you resolve by paging and matching).
  - Only after the project is confirmed, resolve a bare test **name** with `blazemeter_tests list` *within that project_id*.
  - **Name doesn't resolve cleanly:** no match → say so and stop; multiple matches → list each candidate with its parent and id and let the user pick; 403 → report the access gap, don't retry. Never fall back to the default.

Resolve the full hierarchy upward and confirm. Chain these calls — each response provides the ID needed for the next:

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

**AI Consent gate:** if the account has **not** enabled AI consent (from step 4), stop with a clear message — e.g. `Account <name> (<id>) has not enabled AI consent` — rather than running or posting anything.

If any link in the chain fails (e.g. a `project_id` is missing from the test response), **stop and report the gap** — do not gate a PR against an unverified context. Once confirmed, carry this account/workspace forward for later skills in the same conversation (display it, allow a one-step "switch"); this is conversational memory, not stored state.

### Step 0b — Resolve the PR target (GitHub-MCP-first)

Ask for the **GitHub `owner`/`repo`** and the **PR number** (a PR URL gives all three — parse `owner`, `repo`, and the number from it). Then read the PR via the **GitHub MCP** so you operate on a verified PR and capture its **head commit SHA** (you need that SHA to set the commit status in Step 4):

```
pull_request_read  { method: "get", owner: <owner>, repo: <repo>, pullNumber: <number> }
   → captures: PR title, state (open/closed/merged), head ref (branch),
               head SHA (the commit the status attaches to), base ref
```

- If the PR is **closed or merged**, say so and confirm the user still wants to gate it before running (a gate on a closed PR is usually a mistake).
- If the read fails (404 / no access), **stop and report it** — do not guess the repo or commit. GitHub auth is the MCP's; never ask for or handle a token.

> The PR may carry a load profile to apply for this run (e.g. the user says "run it at the PR's load: 50 VU for 5m"). Treat that as the optional load profile in Step 1; if none is given, run the test as-is.

### Step 0c — Display both targets before acting

```
BlazeMeter target
  Test:       <test name>  (ID: <test_id>)
  Project:    <project name>  (ID: <project_id>)
  Workspace:  <workspace name>  (ID: <workspace_id>)
  Account:    <account name>  (ID: <account_id>)

PR target
  Repo:       <owner>/<repo>
  PR:         #<number> — <PR title>  (<open | closed | merged>)
  Head:       <branch>  @ <head SHA short>
```

Let this stand as confirmation. The run in Step 1 **consumes test minutes**, and Step 4 **writes to GitHub**, so don't proceed past here until both targets are confirmed.

## Step 1 — Run the test (delegate to bzm-run-test)

**Delegate to `bzm-run-test`** — follow its procedure, do not re-derive it. In particular:

- **(Optional) load profile:** if the PR specified a load profile, apply it exactly as bzm-run-test's Step 1 describes (`configure_load`, with explicit confirmation, honoring the `hold-for`/`iterations` mutual-exclusion gotcha). If no profile was given, **skip it** and run as-is.
- **Start + poll to completion:** start with `blazemeter_execution start { test_id }`, surface the `execution_url` immediately, and poll `blazemeter_execution read` until **`ended != null`** (completion is `ended` going non-null, not a status string) — exactly as bzm-run-test's Steps 2–3.

Capture the resulting **candidate `execution_id`**. This is the candidate side of the comparison. (You do not need bzm-run-test's full pass/fail report here — the gate's verdict comes from the comparison in Step 3 — but its execution `pass`/`fail` status is still useful context and is carried into the verdict.)

## Step 2 — Resolve the baseline (delegate to bzm-baseline)

**Delegate to `bzm-baseline`** to resolve the active baseline for this test, in its resolution order **pinned → committed `.blazemeter/baseline.json` → last-passing**. Follow that skill's Step 3 exactly; it uses the shared script for the deterministic parts:

1. **Conversational pin** — if the user pinned an `execution_id` earlier in this conversation, that is the baseline.
2. **Committed CI file** — if the repo has a `.blazemeter/baseline.json`, read its entry for this `test_id` with the script (do not hand-parse JSON):

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py resolve \
     --file .blazemeter/baseline.json --test-id <test_id>
   ```

   A **malformed** file exits non-zero — report it and stop, don't fall through silently.
3. **Last passing run** — with no pin and no file entry, list the test's executions (`blazemeter_execution list { test_id, limit: 50, offset: 0 }`, page by `offset`) and let the script pick the most recent passing one:

   ```
   python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py last-passing \
     --executions <executions.json>
   ```

**HONESTY GUARD — no baseline ⇒ cannot gate.** If all three resolve to nothing (no pin, no file entry, and `last-passing` returns `null` because there is no passing run), there is **no baseline to compare against**. **Do not post a green/passing status.** Instead, post the PR comment in its **"no baseline — cannot gate"** form (Output template below) and set the commit status to a **neutral / "cannot gate"** state (never `success`), then stop. A gate with no baseline is honestly *inconclusive*, never a pass.

## Step 3 — Compare candidate vs baseline (delegate to bzm-compare-runs)

**Delegate to `bzm-compare-runs`** with **baseline = the Step 2 execution** and **candidate = the Step 1 execution**. Follow its procedure exactly — do not re-derive the diff math, the thresholds, the normalization, or the verdict:

- It pulls `read_all_reports` for both executions and diffs the KPIs: **avg / p90 / p95 / p99 response time, throughput (RPS), error rate** — with magnitude **and** direction.
- It flags a **regression** when a KPI moves the worse way by **≥ the threshold** (default 10%; let the user override), and calls out any absolute error-rate crossing of 1% / 5%.
- It emits a **ship / no-ship** (or ship-with-caveats / inconclusive) verdict with reasons tied to numbers.

**HONESTY GUARD — load configs differ ⇒ not apples-to-apples.** bzm-compare-runs' Step 2 compares the two runs' **achieved peak concurrency** (`max_concurrent_users`) and, when they differ beyond a small margin, raises a **CONFIG MISMATCH** warning, normalizes throughput as **RPS-per-VU**, and lowers confidence in latency/error-rate deltas (which don't normalize across load levels). **Surface that warning verbatim into the PR comment** — do not bury it. A regression driven by a load-level difference must read as *inconclusive / not apples-to-apples*, never as a clean code regression, and a comparison that isn't apples-to-apples must never post a confident green.

Carry forward from the comparison: the **KPI diff table**, the **regression flags**, the **verdict**, and any **CONFIG MISMATCH** note.

## Step 4 — Post back to the PR (GitHub-MCP-first)

Close the loop on GitHub. Two writes, both reflecting the same verdict:

### 4a — PR comment (GitHub MCP — `add_issue_comment`)

Post the comment with the **GitHub MCP**, MCP-first. A PR's conversation comments are issue comments, so use **`add_issue_comment`** with the PR number as `issue_number`:

```
add_issue_comment {
  owner: <owner>, repo: <repo>,
  issue_number: <PR number>,
  body: <the PR-comment body from the Output template>
}
```

The body contains the verdict, the KPI-diff table (from Step 3), the baseline source/link, the candidate `execution_url`, and — when present — the **CONFIG MISMATCH** warning or the **no-baseline** notice. Never put a credential in the body.

### 4b — Commit status / check on the PR's head commit

Set a status on the **head commit SHA** captured in Step 0b so the verdict shows on the PR's checks:

- **state** = `success` when the verdict is **SHIP** (no regression past threshold, configs comparable, criteria held);
- **state** = `failure` when the verdict is **NO-SHIP** (a regression past threshold, an error-rate incident crossing, or the candidate failed its criteria while the baseline passed);
- **state** = `error` / **neutral "cannot gate"** when the gate is **inconclusive** — specifically the **no-baseline** guard (Step 2) or a **CONFIG MISMATCH** that makes the result not apples-to-apples (Step 3). Never report these as `success`.

Use a stable check **context/name** like `blazemeter/pr-gate` with a one-line description echoing the verdict, and point its target URL at the candidate's `execution_url`.

> **Justified GitHub fallback.** The GitHub MCP exposes **read**-side status/check tools (`pull_request_read` with `method: "get_status"` or `"get_check_runs"`, and `get_check_run`) but **no write tool to create a commit status or check run**. Setting the status is therefore the one documented place this skill drops to the **`gh` CLI / GitHub REST** fallback — exactly the "MCP-first, REST only for a genuine gap, and say so" posture. Create the status against the head SHA, e.g.:
>
> ```
> gh api -X POST repos/<owner>/<repo>/statuses/<head SHA> \
>   -f state=<success|failure|error> \
>   -f context=blazemeter/pr-gate \
>   -f description="<one-line verdict>" \
>   -f target_url=<execution_url>
> ```
>
> This uses the GitHub CLI's already-configured auth; the skill never reads, embeds, echoes, or logs a token. After writing, you may **verify** with `pull_request_read { method: "get_status" }` (MCP) to confirm the status landed.

After both writes, confirm to the user in chat what was posted (comment link + status state) and give the same verdict summary (Output template, chat section).

## Output template

### PR comment (posted via `add_issue_comment`)

```
## BlazeMeter performance gate — <SHIP ✅ | NO-SHIP ❌ | INCONCLUSIVE ⚠️>

**Test:** <test name> (ID: <test_id>)  |  **PR:** #<number> @ <head SHA short>
**Candidate:** exec <candidate_id> — [report](<execution_url>)
**Baseline:** exec <baseline_id> (<pinned | committed .blazemeter/baseline.json | last-passing>)
**Threshold:** regression flagged at ≥ <N>%

> [CONFIG MISMATCH — only if Step 3 found one]
> Baseline achieved peak <X> VU vs candidate <Y> VU — not apples-to-apples.
> Throughput shown as RPS-per-VU; latency/error-rate deltas are lower-confidence.

### KPI diff
| KPI | Baseline | Candidate | Δ | Δ% | Direction | Flag |
|-----|----------|-----------|---|----|-----------|------|
| Avg RT (ms)      |   |   |   |   |   |   |
| p90 RT (ms)      |   |   |   |   |   |   |
| p95 RT (ms)      |   |   |   |   |   |   |
| p99 RT (ms)      |   |   |   |   |   |   |
| Throughput (RPS) |   |   |   |   |   |   |
| RPS per VU       |   |   |   |   |   |   |  ← only when configs differ
| Error rate (%)   |   |   |   |   |   |   |

### Verdict
<1–2 sentences leading with the decisive KPI, citing numbers — from bzm-compare-runs.>

<!-- posted by perforce:bzm-pr-gate -->
```

**No-baseline form** (replaces the KPI table when Step 2's guard fires):

```
## BlazeMeter performance gate — ⚠️ CANNOT GATE (no baseline)

**Test:** <test name> (ID: <test_id>)  |  **PR:** #<number> @ <head SHA short>
**Candidate:** exec <candidate_id> — [report](<execution_url>)

No baseline exists for this test — no conversational pin, no entry in
`.blazemeter/baseline.json`, and no passing run to fall back to. There is nothing to
compare against, so this PR **cannot be gated on performance**. This is **not** a pass.

**To gate:** pin a baseline execution, or commit one to `.blazemeter/baseline.json`
with the `perforce:bzm-baseline` skill, then re-run the gate.

<!-- posted by perforce:bzm-pr-gate -->
```

### Chat summary (to the user)

```
## PR gate posted: #<number> — <owner>/<repo>

- Verdict:  <SHIP | NO-SHIP | INCONCLUSIVE (no baseline) | INCONCLUSIVE (config mismatch)>
- Comment:  <link to the posted PR comment>
- Status:   blazemeter/pr-gate = <success | failure | error> on <head SHA short>
- Candidate: exec <candidate_id> (<execution_url>)   Baseline: exec <baseline_id> (<source>)
- Notes:    <config mismatch / no baseline / candidate failed criteria / closed PR, etc.>
```

## Gotchas

- **Delegate, don't duplicate.** Run, baseline-resolution, and comparison logic each live in their own skill (`bzm-run-test`, `bzm-baseline`, `bzm-compare-runs`). Follow those procedures; if one changes, this Journey inherits the change. Don't paraphrase their thresholds, polling rules, or diff math here.
- **No baseline is NOT a pass.** If nothing resolves (no pin, no committed file entry, no passing run), post the "cannot gate" comment and a non-`success` status — never a false green. This is the most important honesty guard.
- **Config mismatch is NOT a clean regression — or a clean pass.** When achieved peak concurrency differs, surface bzm-compare-runs' CONFIG MISMATCH warning into the PR comment, show throughput as RPS-per-VU, and set the status to inconclusive (`error`/neutral), not `success`. A load-level difference must not masquerade as a code regression, nor be papered over as a pass.
- **Set the status on the HEAD commit SHA, not the branch.** Capture the head SHA from `pull_request_read { method: "get" }` in Step 0b and attach the status to that SHA, so it lands on the exact commit GitHub shows on the PR. A force-push moves the head — re-read the PR if time has passed before posting.
- **Commit-status write is the one justified GitHub fallback.** The GitHub MCP can *read* statuses/checks but not *create* them, so Step 4b uses `gh api`/REST — and says why. Everything else GitHub (reading the PR, posting the comment) stays MCP-first via `pull_request_read` and `add_issue_comment`.
- **Never embed or echo a token.** BlazeMeter auth is the MCP's env vars; GitHub auth is the GitHub MCP / already-configured `gh`. The PR comment, the status, and chat output contain **no** credential — not even a key-file path.
- **A run costs minutes; a write is public.** Step 1 consumes test minutes and Step 4 posts a visible PR comment + status. Confirm both targets in Step 0c first; don't gate a closed/merged PR without asking.
- **Completion before comparison.** The candidate must have `ended != null` before Step 3 — a partial run's KPIs look like a regression. bzm-run-test's polling already enforces this; don't shortcut it.
- **Idempotency on re-runs.** Re-gating the same PR posts another comment and overwrites the `blazemeter/pr-gate` status (same context) on the head SHA. Mention that a fresh comment was added so the user isn't surprised by duplicates.
- **Don't conflate auth files.** A test's asset `auth.json` (authenticating the system under test) is unrelated to Platform Credentials or the GitHub token — never surface any of them in PR output.
