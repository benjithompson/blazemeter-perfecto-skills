# Contributing

Thanks for helping grow the BlazeMeter / Perfecto skills! This repo is a Claude Code **plugin** of
expert-workflow skills. While it's under active development it loads directly from a local git
checkout (a skills-directory plugin — see the README "Install" section); marketplace distribution is
deferred until it's built out further. It goes depth-first on the BlazeMeter **Performance** pillar.

By contributing you agree your contributions are licensed under the repo's
[Apache-2.0 License](./LICENSE).

## Where work comes from

Issues and PRDs live in this repo's **GitHub Issues** (see `docs/agents/issue-tracker.md`). The
umbrella plan is the PRD in issue #1; it is split into independently grabbable issues.

We triage with five canonical labels (full table in `docs/agents/triage-labels.md`):

| Label | Meaning |
| --- | --- |
| `needs-triage` | A maintainer needs to evaluate this issue |
| `needs-info` | Waiting on the reporter for more information |
| `ready-for-agent` | Fully specified — ready for an AFK agent to pick up |
| `ready-for-human` | Requires human implementation |
| `wontfix` | Will not be actioned |

Grab an issue labelled **`ready-for-agent`** or **`ready-for-human`**; it has everything you need
to start.

## Authoring or changing a skill

Read **`shared/conventions.md`** first — it is the house style and the Definition of Done. In
short, every skill:

- lives in `skills/<name>/SKILL.md`, folder name == frontmatter `name` (kebab-case);
- opens with valid frontmatter (`name` + `description`) — **CI lints this**;
- includes the canonical **Context Resolution** Step 0 (resolve and *display*
  account → workspace → project → test before acting);
- is MCP-first, with any REST fallback justified in the prose;
- contains **no** personal absolute paths and **no** credentials.

Shared, deterministic logic (scripts) goes in `shared/scripts/` and is referenced from skills via
`${CLAUDE_PLUGIN_ROOT}`; it gets fixture tests under `tests/`. Skills that ship a static asset (e.g.
the `bzm-report` HTML template) keep it in the skill's own `assets/` and fill it in-skill —
no interpreter shelled out at runtime.

## Running checks locally

```bash
# 1. Frontmatter lint (no dependencies — standard-library Python only)
python shared/scripts/lint_frontmatter.py skills

# 2. Tests for the deterministic layer
python -m pip install -r requirements-dev.txt
pytest
```

CI runs exactly these on every push and pull request (frontmatter lint + script `--help` smoke +
tests). A PR is ready when CI is green and the Definition of Done in `shared/conventions.md` is
satisfied.

## Credentials & safety

Never commit API keys, key files, or `.env` files, and never embed credentials in generated
reports. Skills reuse the BlazeMeter MCP's existing environment variables (see the README and
`shared/conventions.md`).
