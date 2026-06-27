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

This repo is itself an installable plugin (`.claude-plugin/plugin.json`) published via a
self-hosted marketplace (`.claude-plugin/marketplace.json`). Layout:

- `skills/<name>/SKILL.md` — the skills (auto-discovered; invoked as `blazemeter-perfecto:<name>`).
- `shared/conventions.md` — **the skill-authoring house style and Definition of Done. Read it
  before adding or changing a skill.** It defines the required Context Resolution step, frontmatter
  rules, credential handling, and MCP-first integration.
- `shared/scripts/` — deterministic shared scripts (e.g. the frontmatter linter), referenced from
  skills via `${CLAUDE_PLUGIN_ROOT}`.
- `tests/` — fixture-driven tests for the deterministic layer; run with `pytest`.
- `commands/` — optional thin command entry points to skills.

CI (`.github/workflows/ci.yml`) lints every `SKILL.md` frontmatter, smoke-tests shared scripts'
`--help`, and runs the tests.

## Making changes take effect (release ritual)

A Claude Code plugin install is **version-pinned**: a running install only picks up new or changed
skills/commands when the plugin's `version` increases. Editing files in this repo does **not**
update anyone's install on its own.

So **every change that should ship must bump the version** — and `.claude-plugin/plugin.json` and
`.claude-plugin/marketplace.json` must carry the **same** version. This is the mechanism; without
it the change is invisible to installs (this is exactly why all but one skill went unseen until
0.2.0).

After the change merges, the plugin must be **reinstalled and reloaded** for the new version to
take effect in a Claude Code session:

```text
/plugin install blazemeter-perfecto@blazemeter-perfecto-skills
/reload-plugins      # or restart Claude Code
```

These are **interactive Claude Code commands** — they run inside a live session, not from a shell,
CI, or a git hook, so they **cannot be auto-run by the merge itself** (and the agent has no tool to
invoke them). The achievable automation is the version bump (enforced via the Definition of Done in
`shared/conventions.md`) plus this ritual: **when an agent lands a plugin change in a session, end
by surfacing the two commands above for the user to run**; human maintainers run them after pulling.

## Agent skills

### Issue tracker

Issues and PRDs live in this repo's GitHub Issues (via the `gh` CLI). External PRs are not a
triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, used as-is: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
