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
  a cache, so outside references break. (Prose links to docs like `shared/conventions.md` are
  fine; this rule is about paths the skill actually executes or reads.)

## 2. Frontmatter (enforced by CI)

Each `SKILL.md` opens with flat YAML frontmatter. CI runs
`shared/scripts/lint_frontmatter.py` over every skill; these rules are what it checks:

```markdown
---
name: bzm-analyze-test
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
`perforce:<skill-name>` (e.g. `perforce:bzm-analyze-test`). Name
skills platform-first (`bzm-analyze-test`, not `analyze-test`) so later pillars
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

1. Start from the default. `blazemeter_user read` gives **one** default account/workspace/project.
   Present it as a **pre-filled choice to confirm or override** — only prompt when there is genuine
   ambiguity. If a level has exactly one option, proceed and just **display** it.
2. To enumerate options, list the level (`blazemeter_account list`, `blazemeter_workspaces list`,
   `blazemeter_project list`, `blazemeter_tests list`) one page at a time (`limit: 50`).
   - **Small set** (the first page is *not* full) → show a **numbered list, every entry with its
     id** — e.g. `1. Acme (account 12345)` — and let the user pick.
   - **Too big to list** (the first page comes back full, so more pages exist — common for users
     with very many workspaces) → **do not dump the list**. Ask the user to **name or paste** the
     workspace/project/test. A pasted **id short-circuits** any level (direct `read`); a **name**
     you resolve by paging and matching.
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
`analyze` / `run` / `compare` / `triage` operate on one test (or one execution of it), so they use
§4 exactly as written.

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
3. **Enumerate the tests in that scope** by paging `blazemeter_tests list` (`limit: 50`) to
   completion. Enumeration is the point here, so a full first page is **not** a reason to ask the
   user to name one test — keep paging and operate over the whole set. (If the scope is so large
   that enumerating is impractical, say so and ask the user to **narrow the scope** to a specific
   project — never silently truncate to "the first page".)
4. **Honor the per-account AI Consent gate** (§4.4) on the resolved account before enumerating, the
   same as the single-test step.
5. **Display the resolved scope and the count of tests it covers** before acting — the cross-test
   analogue of §4.5 — so the run is auditable and the user sees what is in scope:

   ```
   Scope:      Project <project name>  (ID: <project_id>)
   Workspace:  <workspace name>  (ID: <workspace_id>)
   Account:    <account name>  (ID: <account_id>)
   Tests:      <N> tests in scope
   ```

6. **Carry the resolved scope forward** as conversational memory and **never persist it** (§4.6),
   exactly as the single-test step carries the account/workspace forward.

In short: single-test skills resolve **to a test**; cross-test skills resolve **to a scope and
enumerate its tests**. Same don't-assume guarantees, one fewer level of narrowing.

## 5. Integration: MCP-first, REST as a documented fallback

- Prefer the **BlazeMeter MCP** tools (`blazemeter_*`). The
  [bzm-mcp repo](https://github.com/Blazemeter/bzm-mcp) is the source of truth for what the MCP
  can do.
- Fall back to the **BlazeMeter REST API v4** only to fill a genuine MCP gap, and when you do,
  **say so and why** in the skill. Reason from the
  [v4 explorer](https://a.blazemeter.com/api/v4/explorer/), not from memory.
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

`bzm-analyze-test` is the reference implementation of this shape.

## 8. Definition of Done

Before a skill merges:

- [ ] Frontmatter passes `lint_frontmatter.py` (CI enforces this).
- [ ] Folder name == `name` == namespaced invocation works.
- [ ] Step 0 Context Resolution is present and **displays** the resolved hierarchy (with ids).
- [ ] Step 0 never assumes a single default: it confirms/overrides the default, applies the tiered
      pick rule (list small sets, ask-by-name when too big to list), disambiguates name collisions
      across accounts, checks the per-account **AI Consent** gate, and carries context forward within
      the conversation without persisting it.
- [ ] No personal absolute paths, no credentials, no machine-specific config anywhere.
- [ ] Any shared script is in `shared/scripts/` and referenced via `${CLAUDE_PLUGIN_ROOT}`.
- [ ] Uses MCP-first; any REST usage is justified in the prose.
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
