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

## Agent skills

### Issue tracker

Issues and PRDs live in this repo's GitHub Issues (via the `gh` CLI). External PRs are not a
triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, used as-is: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
