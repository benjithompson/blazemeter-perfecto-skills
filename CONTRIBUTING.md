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

Shared, deterministic logic (scripts, the report renderer) goes in `shared/scripts/` and is
referenced from skills via `${CLAUDE_PLUGIN_ROOT}`; it gets fixture tests under `tests/`.

## Running checks locally

```bash
# 1. Frontmatter lint (no dependencies — standard-library Python only)
python shared/scripts/lint_frontmatter.py skills

# 2. Tests for the deterministic layer (mock/unit only by default)
python -m pip install -r requirements-dev.txt
pytest
```

CI runs exactly these on every push and pull request (frontmatter lint + script `--help` smoke +
tests). A PR is ready when CI is green and the Definition of Done in `shared/conventions.md` is
satisfied.

### Live integration tests (optional, local only)

Plain `pytest` runs only the hermetic mock tests. The `bzm_*` REST utilities also have **live**
integration tests that hit real BlazeMeter end-to-end — these replace the old "run it once by
hand" verify step (see `docs/adr/0013-live-integration-tests.md`). They are deselected by default
and **skip** unless credentials and target ids are configured, so they never break CI.

To run them locally:

```bash
# Configure once: copy the template and fill in (the file is gitignored), OR export the vars.
cp tests/live.env.example tests/live.env

# Credentials reuse the MCP's scheme (API_KEY_ID + API_KEY_SECRET, or BLAZEMETER_API_KEY → key
# file). A gitignored api-key.json at the repo root is also picked up automatically.
# Targets: BZM_LIVE_EXECUTION_ID (an execution that produced artifacts) and BZM_LIVE_TEST_ID
# (a throwaway test to upload an auth.json asset to).

pytest -m live          # run only the live tests
pytest -m live -v       # ...verbose, to see which skipped vs ran
```

Any live target that isn't configured simply skips. We do **not** store a BlazeMeter key as a CI
secret; live coverage stays a local opt-in.

## Credentials & safety

Never commit API keys, key files, or `.env` files, and never embed credentials in generated
reports. Skills reuse the BlazeMeter MCP's existing environment variables (see the README and
`shared/conventions.md`).
