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
name: analyze-blazemeter-test
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

The plugin is named `blazemeter-perfecto`, so skills are invoked namespaced as
`blazemeter-perfecto:<skill-name>` (e.g. `blazemeter-perfecto:analyze-blazemeter-test`). Name
skills platform-first (`analyze-blazemeter-test`, not `analyze-test`) so later pillars
(Perfecto, Virtual Services, API Monitoring) never collide.

## 4. The canonical Context Resolution step (required)

**Every skill that touches a BlazeMeter account must resolve and *display* its full context —
account → workspace → project → test — before it acts, and stop rather than guess if it can't.**
This is the single most important safety rule: it stops a skill from analyzing, comparing, or
running against the wrong thing.

Embed this as **Step 0** of the skill (skills are self-contained prose — copy and adapt it):

1. Determine the target from what the user gave you:
   - a `test_id` → use it directly;
   - a test **name** → `blazemeter_tests list` within the relevant `project_id` to resolve it;
   - **nothing** → `blazemeter_user read` for the default account/workspace/project, then list
     tests and let the user pick.
2. Chain reads to resolve the full hierarchy (each response yields the id for the next):
   `blazemeter_tests read` → `blazemeter_project read` → `blazemeter_workspaces read` →
   `blazemeter_account read`.
3. **Display** the resolved context and let it stand as confirmation before continuing:

   ```
   Test:       <test name>  (ID: <test_id>)
   Project:    <project name>
   Workspace:  <workspace name>
   Account:    <account name>
   ```

4. If **any** link fails (a missing `project_id`, an unresolved name), **stop and report the
   gap** — never proceed against an unverified context.

Respect the hierarchy **Account → Workspace → Project → Test → Execution**; validate each level
before operating on the next.

## 5. Integration: MCP-first, REST as a documented fallback

- Prefer the **BlazeMeter MCP** tools (`blazemeter_*`). The
  [bzm-mcp repo](https://github.com/Blazemeter/bzm-mcp) is the source of truth for what the MCP
  can do.
- Fall back to the **BlazeMeter REST API v4** only to fill a genuine MCP gap, and when you do,
  **say so and why** in the skill. Reason from the
  [v4 explorer](https://a.blazemeter.com/api/v4/explorer/), not from memory.

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

`analyze-blazemeter-test` is the reference implementation of this shape.

## 8. Definition of Done

Before a skill merges:

- [ ] Frontmatter passes `lint_frontmatter.py` (CI enforces this).
- [ ] Folder name == `name` == namespaced invocation works.
- [ ] Step 0 Context Resolution is present and **displays** the resolved hierarchy.
- [ ] No personal absolute paths, no credentials, no machine-specific config anywhere.
- [ ] Any shared script is in `shared/scripts/` and referenced via `${CLAUDE_PLUGIN_ROOT}`.
- [ ] Uses MCP-first; any REST usage is justified in the prose.
- [ ] Has an output template and a Gotchas section.
- [ ] Deterministic shared logic (renderers, credential resolution) has fixture tests; ran once
      manually against a real BlazeMeter test.
