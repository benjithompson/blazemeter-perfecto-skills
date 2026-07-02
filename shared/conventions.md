# Skill-authoring conventions (house style)

Every skill in this plugin follows the same shape so that they read consistently, resolve
context safely, and pass CI. This is the standard referenced by the Definition of Done and by
each `SKILL.md`. Read it before adding or editing a skill.

> Audience and scope follow the PRD (issue #1): skills are built for **Platform Customers**
> (QA / performance engineers with working BlazeMeter accounts) and **Field Teams**, not for
> onboarding-from-nothing. Skills encode *expertise*, not one-wrapper-per-MCP-operation.

## 1. Where things live

```
.claude-plugin/
  plugin.json          # plugin manifest (name = namespace)
  marketplace.json     # self-hosted marketplace listing this plugin
skills/<skill-name>/
  SKILL.md             # one skill; folder name == frontmatter `name`
shared/
  conventions.md       # this file — the house style
  scripts/             # deterministic scripts, shared across skills
commands/              # thin command entry points to skills (optional)
tests/                 # fixture-driven tests for the deterministic layer
```

- Skills are **auto-discovered** from `skills/` — you do not list them in `plugin.json`.
- A script or file a skill **opens at runtime** is referenced via **`${CLAUDE_PLUGIN_ROOT}`**
  (e.g. `${CLAUDE_PLUGIN_ROOT}/shared/scripts/foo.py`). Never use an absolute path from your own
  machine, and never reference files outside the plugin root — an installed plugin is copied into
  a cache, so outside references break. (This rule is about paths the skill executes or reads at
  runtime; §9 separately bans *any* reference — path or prose — to contributor docs from
  user-facing surfaces.)

## 2. Frontmatter (enforced by CI)

Each `SKILL.md` opens with flat YAML frontmatter. CI runs
`shared/scripts/lint_frontmatter.py` over every skill; these rules are what it checks:

```markdown
---
name: bzm-test-analysis
description: One or two sentences — what the skill does AND when to use it ("Use when …") — so the model can decide to invoke it.
---
```

- The opening `---` must be the **very first line** — no stray characters (a leading backtick
  once shipped and broke the original skill), no BOM, no blank line before it.
- Each field is a **single line** of flat `key: value`; the linter treats every non-blank line in
  the block as one field (no YAML continuation, block scalars, or nesting).
- `name` is **required**, **kebab-case**, and must **equal the skill's directory name**.
- `description` is **required**, non-empty, and ≤ 1024 characters.
- Optional keys (`allowed-tools`, `disable-model-invocation`, …) are allowed and ignored by the
  linter; keep them flat `key: value` too.

Run the linter locally before you push:

```bash
python shared/scripts/lint_frontmatter.py skills
```

## 3. Namespacing

The plugin is named `perforce`, so skills are invoked namespaced as
`perforce:<skill-name>` (e.g. `perforce:bzm-test-analysis`). Name
skills platform-first (`bzm-test-analysis`, not `test-analysis`) so later pillars
(Perfecto, Virtual Services, API Monitoring) never collide.

## 4. The canonical Context Resolution step (required)

**Every skill that touches a BlazeMeter account must resolve and *display* its full context —
account → workspace → project → test — before it acts, and stop rather than guess if it can't.**
This is the single most important safety rule: it stops a skill from analyzing, comparing, or
running against the wrong thing.

The guiding principle is **don't assume**: a user can belong to **multiple accounts**, each with
**multiple workspaces, projects, and tests**, and names collide across them. The default from
`blazemeter_user read` is a **suggestion to confirm, never a decision made for the user**. No skill
silently picks a level when more than one is possible.

Embed this as **Step 0** of the skill (skills are self-contained prose — copy and adapt it).

### 4.1 Two entry paths

- **A `test_id` was given** → trust it and resolve *upward*: `blazemeter_tests read` →
  `blazemeter_project read` → `blazemeter_workspaces read` → `blazemeter_account read` (each
  response yields the id for the next). The displayed context block (§4.4) stands as confirmation;
  no menu needed.
- **Nothing was given, or only a test *name*** → resolve *top-down*: establish the account, then the
  workspace, then the project (§4.2), **then** look up a bare test name only inside that confirmed
  project. A name is meaningless without a scope — never search a name across everything.

### 4.2 Picking a level — one uniform tiered rule

Apply the same rule at **every** level you must resolve (workspace, project, test):

1. Start from the default, but **determine ambiguity by enumerating — never assume the default is
   the only option**. `blazemeter_user read` gives **one** default account/workspace/project; treat
   it as a pre-filled suggestion, then list the level (step 2) to see how many options exist.
   **Exactly one** → display it and proceed. **More than one** → present the numbered pick and
   **stop** for the user's choice; never silently accept the default.
2. To enumerate options, list the level (`blazemeter_account list`, `blazemeter_workspaces list`,
   `blazemeter_project list`, `blazemeter_tests list`) one page at a time (`limit: 50`), then
   present them with a **preference for an interactive choice list**:
   - **Fits a choice list** (a handful of options) → present an **interactive choice list**, every
     entry showing its **name + id** with the default marked; the user clicks one.
   - **Too many for the choice widget but still enumerable** (within a page or two) → fall back to a
     **numbered text list with ids** — e.g. `1. Acme (account 12345)` — the user picks a number or
     pastes an id.
   - **Large / paginated** (the first page comes back full, so more pages exist — common for users
     with very many workspaces, >50) → **do not dump the list**. Ask the user to **name, paste an
     id, or give a filter**. A pasted **id short-circuits** any level (direct `read`); a **name**
     you resolve by paging and matching — except a **test** name, which resolves with one
     `blazemeter_tests search` (`test_name` + `account_id`, **scoped to the already-confirmed
     levels** via `workspace_id_list`/`project_id_list` — never unscoped across the account, per
     §4.1). Two search quirks: pass a **full-history** `custom` window (e.g. `start_time`
     2000-01-01 → today) because the default `time_frame` only matches tests created **today**;
     and results come 50/page — if `has_more` is true, page on or ask for a narrower fragment
     before presenting the multiple-matches menu. Workspaces, projects, and executions have no
     usable name search (execution names are just the test's display name, and execution search
     rows carry no test id or status) — those you page and match.
3. Always show the **id** next to each name so same-named entities are distinguishable and the id is
   locked for the next call.

### 4.3 Failure handling — strict, never assume

When a user-supplied name does not resolve cleanly, **stop — never fall back to the default**:

- **No match** → say so, show what *is* available (or ask for the id), and stop.
- **Multiple matches** → this is itself a disambiguation menu: list every candidate with its
  **parent and id** (e.g. `Staging — account Acme (ws 123)` / `Staging — account Globex (ws 456)`),
  and let the user pick.
- **No access** (a `read` returns 403) → report the access gap plainly; do not retry.
- **Broken upward link** (e.g. a `test_id` whose response is missing `project_id`) → stop and
  report the gap.

### 4.4 AI Consent gate

AI access is gated **per account** (the consent flag lives on the account object). After resolving
the account, check its AI-consent state via `blazemeter_account read`. If the account has **not**
consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` —
rather than letting a downstream tool fail cryptically.

### 4.5 Display and confirm

**Display** the resolved context and let it stand as confirmation before continuing — including when
a level was resolved by name rather than picked from a list:

```
Test:       <test name>  (ID: <test_id>)
Project:    <project name>  (ID: <project_id>)
Workspace:  <workspace name>  (ID: <workspace_id>)
Account:    <account name>  (ID: <account_id>)
```

### 4.6 Carry context forward (within a conversation)

Resolve the scope **once**, then **carry the confirmed account/workspace forward** across later
skills in the same conversation — **display it each time** with a one-step override (e.g. "say
*switch* to change"). Re-prompting identical context on every invocation breeds banner blindness,
which is itself a safety risk. This is **conversational memory, not stored state**: never write the
chosen context to disk or cache it across sessions.

Respect the hierarchy **Account → Workspace → Project → Test → Execution**; validate each level
before operating on the next.

### 4.7 Cross-test variant — resolve to a *scope*, then enumerate the tests within it

§4.1–4.6 resolve **down to a single test**. That is the right shape for **single-test skills** —
`test-analysis` / `run-test` / `triage-failure` operate on one test (or one execution of it), so
they use §4 exactly as written.

Some skills instead operate over **many tests at once** — e.g. `daily-digest` and `portfolio-report`,
which summarize or roll up every test in a workspace or project. These use this **cross-test
variant**: resolve **account → workspace → project**, then **enumerate the tests within that
confirmed scope** instead of picking one.

The variant **reuses every guarantee** of the single-test step — it changes only the final level:

1. **Resolve account → workspace → project** with the **same tiered pick rule** (§4.2) at each
   level: confirm-or-override the default, list small sets as a numbered menu with ids, and
   **ask the user to name or paste** the workspace/project when the first page comes back full.
   A pasted **id short-circuits** a level. Apply the **same strict failure handling** (§4.3) — no
   silent fall-back to the default at any level.
2. **Choose the scope to roll up over.** The resolved scope is either the **project** (roll up its
   tests) or the **workspace** (roll up across all its projects), depending on what the skill
   declares. Stop at that level — **do not** descend to a single test.
3. **Census the window, don't walk the catalog** (ADR-0019, window-first amendment). Do **not**
   page `blazemeter_tests list` over the scope — activity, not catalog size, is the cost driver.
   Run the MCP window census: one `blazemeter_execution search` (`account_id` always;
   `workspace_id_list`/`project_id_list` for narrower scopes; **always pass `time_frame`
   explicitly** — the default `latest` covers only today) and read the response's **`total`** as
   runs-in-window. Window filtering is day-granular: presets snap the start to midnight, and a
   `custom` window snaps both bounds to midnight with the **end day exclusive** — pass `end_time`
   as the day *after* the window end, or the final day's runs are dropped.
   The rows are discovery metadata only (no test ids, verdicts, or KPIs) — the sweep computes
   `tests_ran` and everything downstream. A large census (hundreds of in-window runs) is a reason
   to **offer narrowing** the scope or shortening the window — never silently truncate.
4. **Honor the per-account AI Consent gate** (§4.4) on the resolved account before invoking the
   engine, the same as the single-test step.
5. **Display the resolved scope and the window census** before acting — the cross-test analogue
   of §4.5 — so the run is auditable and the user sees what is in scope:

   ```
   Scope:      Project <project name>  (ID: <project_id>)
   Workspace:  <workspace name>  (ID: <workspace_id>)
   Account:    <account name>  (ID: <account_id>)
   Window:     <from> → <to>
   Activity:   <N> runs in the window
   ```

6. **Carry the resolved scope forward** as conversational memory and **never persist it** (§4.6),
   exactly as the single-test step carries the account/workspace forward.

In short: single-test skills resolve **to a test**; cross-test skills resolve **to a scope and
census its window**. Same don't-assume guarantees, one fewer level of narrowing.

## 5. Integration: MCP for the control plane, deterministic scripts for bulk data pulls

- Prefer the **BlazeMeter MCP** tools (`blazemeter_*`) for the **control plane**: Step 0 scope
  resolution and its interactive picks, the AI-consent gate, anything that mutates, and
  **single-object drill-ins** (one test, one execution's reports when the user asks about *that*
  run). Since MCP v1.3.0 the control plane also includes **account-wide discovery**: the `search`
  actions on `blazemeter_tests` and `blazemeter_execution` (name lookups, window censuses via the
  response's `total`). Their rows are discovery metadata only — no test ids on execution rows, no
  verdicts, no KPIs — so they do not move the data-plane line below. The
  [bzm-mcp repo](https://github.com/Blazemeter/bzm-mcp) is the source of truth for what the MCP
  can do.
- **Bulk data-plane reads go to the shared deterministic engine, not the MCP** (ADR-0019). The
  line is **structural, decidable before the first call**: any *data-driven fan-out* — "for each
  X, list/read Y" where the iteration count comes from data rather than from the user's pick —
  runs via `shared/scripts/bzm_fetch.py`, which sweeps the REST API v4 directly and returns one
  compact pre-aggregated JSON (size O(tests)). No "small scope, MCP is fine" branch: one code
  path, fixture-tested. Chained MCP loops over tests/executions/reports are a review flag.
- The engine's REST usage is governed by `shared/scripts/API_NOTES.md` (distilled from the
  bzm-mcp source + the [v4 explorer](https://a.blazemeter.com/api/v4/explorer/) — never from
  memory, and never by vendoring the swagger). Any *other* REST call still needs a genuine MCP
  gap, **said so and why** in the skill.
- **GitHub integration is MCP-first too.** A skill that touches GitHub (PR comments, commit status)
  uses the **GitHub MCP**, mirroring the BlazeMeter posture above; `gh`/REST is only a documented
  fallback for a genuine gap. Generated CI artifacts (GitHub Actions YAML) are **secrets-only**:
  they read the BlazeMeter key from `${{ secrets.BLAZEMETER_API_KEY }}` and contain only
  `secrets.*` references — the plugin never embeds, logs, or echoes a token. See ADR-0016.

## 6. Credentials (never committed, never embedded)

Skills reuse the **same environment variables the BlazeMeter MCP already uses** — no second
setup, no repo-specific config, no hardcoded key path:

- `API_KEY_ID` + `API_KEY_SECRET` (preferred), else
- `BLAZEMETER_API_KEY` — a path to a key file.

Keys are **never** committed to the repo and **never** embedded in generated artifacts (reports,
logs). A test's asset `auth.json` (used to authenticate the system under test) is a different
thing from these Platform Credentials — don't conflate them.

## 7. Skill structure (sections, in order)

A `SKILL.md` body, after the frontmatter, reads top-to-bottom as a procedure:

1. **One-line intent** — what assessment/artifact the skill produces.
2. **Step 0 — Context Resolution** (§4 above), always first.
3. **Numbered steps** — the actual workflow (collect → analyze → deliver). Chain MCP calls and
   note where calls can run in parallel.
4. **Output template** — a fixed, copyable structure for what the skill emits, so results are
   consistent run-to-run.
5. **Gotchas** — the non-obvious failure modes (pagination limits, statuses to skip, label
   fields to use instead of raw ids, normalization caveats).

`bzm-test-analysis` is the reference implementation of this shape.

## 8. Definition of Done

Before a skill merges:

- [ ] Frontmatter passes `lint_frontmatter.py` (CI enforces this).
- [ ] User-facing surfaces pass `lint_user_facing.py` — no contributor-doc references (this file,
      ADRs, issues/PRDs, CLAUDE.md/CONTEXT.md) in any `SKILL.md`, command, or runtime asset
      (§9; CI enforces this).
- [ ] Folder name == `name` == namespaced invocation works.
- [ ] Step 0 Context Resolution is present and **displays** the resolved hierarchy (with ids).
- [ ] Step 0 never assumes a single default: it confirms/overrides the default, applies the tiered
      pick rule (list small sets, ask-by-name when too big to list), disambiguates name collisions
      across accounts, checks the per-account **AI Consent** gate, and carries context forward within
      the conversation without persisting it.
- [ ] No personal absolute paths, no credentials, no machine-specific config anywhere.
- [ ] Any shared script is in `shared/scripts/` and referenced via `${CLAUDE_PLUGIN_ROOT}`.
- [ ] MCP for control-plane, `bzm_fetch.py` for data-driven fan-outs (§5); any other REST usage
      is justified in the prose.
- [ ] Has an output template and a Gotchas section.
- [ ] Any deterministic shared logic (e.g. the frontmatter linter) has fixture tests under `tests/`;
      the skill itself was run once against a real BlazeMeter test. A skill that ships a static asset
      (e.g. an HTML template the skill fills in) needs no interpreter and is verified by opening a
      generated artifact.
- [ ] Loads via the direct-from-git (skills-dir) setup — `git pull` + `/reload-plugins` picks the
      change up; no version bump needed for the dev loop. **Only bump the plugin `version`** (same
      value in `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json`) when preparing a
      *marketplace* release, since that path is version-pinned (see CLAUDE.md → "Making changes take
      effect").

## 9. Skills are user-facing — keep contributor context out of them (enforced by CI)

A `SKILL.md` body, a command file, and every asset a skill opens at runtime (e.g. the report
template) are **loaded into end users' Claude sessions**. Anything written there ships as
product. Contributor context — this file, `docs/adr/`, GitHub issues/PRDs, `CLAUDE.md`,
`CONTEXT.md`, `docs/agents/`, CI internals — is the development environment and must never leak
into those surfaces:

- **Inline the rule, not its provenance.** Skills state their rules as self-contained prose
  ("Always resolve and **display** the full context…"), never as citations ("per conventions §4",
  "ADR-0017"). A citation either leaks contributor context or, worse, sends the user's agent off
  to read contributor docs mid-session.
- **What a skill MAY reference:** its own assets and shared scripts via `${CLAUDE_PLUGIN_ROOT}`,
  sibling skills by name (`bzm-set-baseline`), MCP tools, and the user's own repo files
  (`.blazemeter/baseline.json`).
- **Traceability lives on the dev side.** This file and the ADRs describe which skills embed
  which rule; PRs link the ADR. When a convention changes, grep the skills for the embedded
  phrasing and update them — the duplication is deliberate and the price of clean shipping
  surfaces.
- **CI enforces it:** `shared/scripts/lint_user_facing.py` scans `skills/` and `commands/` for
  contributor-doc references and fails the build on any hit. Run it locally:

  ```bash
  python shared/scripts/lint_user_facing.py skills commands
  ```
