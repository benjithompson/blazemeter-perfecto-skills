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

## Agent skills

### Issue tracker

Issues and PRDs live in this repo's GitHub Issues (via the `gh` CLI). External PRs are not a
triage surface. See `docs/agents/issue-tracker.md`.

### Triage labels

Five canonical triage labels, used as-is: `needs-triage`, `needs-info`, `ready-for-agent`,
`ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: one `CONTEXT.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
