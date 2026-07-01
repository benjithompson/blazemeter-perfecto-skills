# BlazeMeter & Perfecto Skills

A shared repository of Claude Code **skills and commands** that help users get more value from
Perforce's testing platforms through their MCP servers:

- **BlazeMeter MCP** — performance / load testing, plus **Virtual Services** and
  **API Test & Monitoring**
- **Perfecto MCP** — mobile / web device testing and execution

The goal is a polished, well-documented set of skills and commands that other users can install
and use to drive these platforms from Claude Code.

> Domain language and architectural decisions are captured lazily in `CONTEXT.md` and
> `docs/adr/` as they get resolved (via the engineering skills). Don't pre-populate them.

## Repository is a Claude Code plugin

This repo is itself a Claude Code plugin (`.claude-plugin/plugin.json`). **During active
development it loads directly from a local git checkout** as a skills-directory plugin
(`perforce@skills-dir`) — symlink the repo into `~/.claude/skills/` and Claude Code
discovers it in place, with no marketplace install and no version pin (see the README "Install"
section). Marketplace distribution (`.claude-plugin/marketplace.json`) is **deferred** until the
plugin is built out further; the manifest is kept ready for that. Layout:

- `skills/<name>/SKILL.md` — the skills (auto-discovered; invoked as `perforce:<name>`).
- `shared/conventions.md` — **the skill-authoring house style and Definition of Done. Read it
  before adding or changing a skill.** It defines the required Context Resolution step, frontmatter
  rules, credential handling, and MCP-first integration.
- `shared/scripts/` — deterministic shared scripts (e.g. the frontmatter linter), referenced from
  skills via `${CLAUDE_PLUGIN_ROOT}`.
- `tests/` — fixture-driven tests for the deterministic layer; run with `pytest`.
- `commands/` — optional thin command entry points to skills.
- `.mcp.json` — bundles the BlazeMeter MCP server (launched via `uvx`) so enabling the plugin
  auto-connects it; ships only env-var placeholders (`${BLAZEMETER_API_KEY}`), never secrets. A
  manually-configured BlazeMeter MCP is deduped by endpoint (higher scope wins). Perfecto is not
  bundled yet (no Perfecto skills). See ADR-0015.

CI (`.github/workflows/ci.yml`) lints every `SKILL.md` frontmatter, smoke-tests shared scripts'
`--help`, and runs the tests.

## Making changes take effect

With the **direct-from-git (skills-dir)** setup, the plugin is read **in place** from your checkout,
so there is no version pin and no reinstall. Updating is just:

```bash
git -C ~/.claude/skills/perforce pull
```

Then `/reload-plugins` (or a new session). Edits to a `SKILL.md` take effect immediately; changes
to other components (`.mcp.json`, `hooks/`, `agents/`, …) need the reload. `/reload-plugins` is an
interactive Claude Code command — when an agent lands a plugin change in a session, end by telling
the user to run it (the agent has no tool to invoke it).

> **Versioning only matters for the deferred marketplace path.** A *marketplace* install is
> version-pinned (it copies the plugin into a cache and only updates when the plugin `version`
> increases, with `.claude-plugin/plugin.json` and `.claude-plugin/marketplace.json` carrying the
> same value). That bites only if/when we publish via the marketplace — it's irrelevant to the
> skills-dir dev loop above. Bump the version when preparing a marketplace release, not for every
> edit.

## Agent skills

### Issue tracker

Issues and PRDs live in this repo's GitHub Issues (via the `gh` CLI). External PRs are not a
triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, used as-is: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
