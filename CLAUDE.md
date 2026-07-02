# BlazeMeter & Perfecto Skills

Claude Code **skills and commands** for driving Perforce's testing platforms through their MCP
servers: **BlazeMeter MCP** (performance/load testing, plus Virtual Services and API Test &
Monitoring) and **Perfecto MCP** (mobile/web device testing). Goal: a polished, well-documented
skill set other users can install and drive from Claude Code.

> Domain language and architectural decisions are captured lazily in `CONTEXT.md` and
> `docs/adr/` as they get resolved. Don't pre-populate them.

## Repository is a Claude Code plugin

The repo is itself the plugin (`.claude-plugin/plugin.json`). During active development it loads
**directly from a local git checkout** as a skills-directory plugin (`perforce@skills-dir`) —
symlink the repo into `~/.claude/skills/`; no marketplace install, no version pin (see README
"Install"). Marketplace distribution is **deferred**; `.claude-plugin/marketplace.json` is kept
ready. Layout:

- `skills/<name>/SKILL.md` — the skills (auto-discovered; invoked as `perforce:<name>`).
- `shared/conventions.md` — **house style + Definition of Done. Read it before adding or changing
  a skill.** Defines the Context Resolution step, frontmatter rules, credential handling, and
  MCP-first integration.
- `shared/scripts/` — deterministic shared scripts, referenced from skills via
  `${CLAUDE_PLUGIN_ROOT}`.
- `shared/assets/` — shared static assets (the branded report template), filled in-skill.
- `tests/` — fixture-driven tests for the deterministic layer; run with `pytest`.
- `commands/` — optional thin command entry points to skills.
- `.mcp.json` — bundles the BlazeMeter MCP server (via `uvx`) so enabling the plugin
  auto-connects it; ships only env-var placeholders (`${BLAZEMETER_API_KEY}`), never secrets.
  Deduped by endpoint against a manually-configured server (higher scope wins). Perfecto is not
  bundled yet. See ADR-0015.

CI (`.github/workflows/ci.yml`): frontmatter lint, user-facing-surface lint (no contributor-doc
references in skills/commands), script `--help` smoke, tests.

## Making changes take effect

Skills-dir load reads the checkout in place: `git -C ~/.claude/skills/perforce pull`, then
`/reload-plugins` (or a new session). `SKILL.md` edits are live immediately; other components
(`.mcp.json`, `shared/`, `hooks/`, …) need the reload. `/reload-plugins` is interactive — an
agent that lands a plugin change must end by telling the user to run it. Bump the plugin
`version` only when preparing a marketplace release (marketplace installs are version-pinned;
irrelevant to the skills-dir dev loop).

## Workflow: PRD → issues → PRs — and keep them current

- **New work starts as a PRD issue**, split into small, independently-grabbable child issues,
  each with acceptance criteria and "blocked by" edges. That granularity is what makes AI-agent
  work effective — agents pick up `ready-for-agent` issues, not prose plans.
- **Landing a PR includes its paper trail.** In the same change, update every doc that states the
  old behavior (README, CONTRIBUTING, `shared/conventions.md`, `CONTEXT.md`) and add
  status/amendment notes to superseded ADRs.
- **Close what shipped.** Close an issue when its work lands, with a comment mapping spec →
  shipped form where they diverged; when a parent PRD closes, verify its delivered children are
  closed too.
- Stale docs and open-but-shipped issues mislead the next agent — treat them as bugs and sweep
  them periodically.

## Agent skills

- **Issue tracker** — issues and PRDs live in this repo's GitHub Issues (via the `gh` CLI).
  External PRs are not a triage surface. See `docs/agents/issue-tracker.md`.
- **Triage labels** — five canonical labels, used as-is: `needs-triage`, `needs-info`,
  `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.
- **Domain docs** — single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See
  `docs/agents/domain.md`.
