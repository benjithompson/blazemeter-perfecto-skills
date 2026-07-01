# Skills are user-facing surfaces: no contributor-doc references ship in them

The repo is two environments in one checkout. The **development environment** is everything a
contributor reads: `CLAUDE.md`, `CONTEXT.md`, `shared/conventions.md`, `docs/adr/`,
`docs/agents/`, GitHub issues/PRDs, `tests/`, CI. The **shipping surface** is everything an end
user's Claude session loads at runtime: every `skills/<name>/SKILL.md`, every file under
`commands/`, and every asset a skill opens while running (the report template, shared scripts'
CLI output).

Until now the two leaked into each other: skills cited their own provenance — "the canonical
Context Resolution step from `shared/conventions.md` §4", "(ADR-0017)", "MCP-first per
conventions §5" — and `commands/README.md` cited "the PRD (issue #1)". Those citations ship. At
best they are noise in a customer's session; at worst the user's agent follows the reference and
pulls contributor instructions (dev loop, triage labels, release process) into a session that
should only contain the product.

**Decision — skills are self-contained product prose.** A user-facing surface states its rules
inline and completely ("Always resolve and **display** the full context…") and never cites where
the rule came from. Permitted references: the skill's own assets and shared scripts via
`${CLAUDE_PLUGIN_ROOT}`, sibling skills by name, MCP tools, and files in the *user's* repo
(`.blazemeter/baseline.json`). Forbidden: the conventions doc, ADRs, section-sign citations,
issues/PRDs, `CLAUDE.md`/`CONTEXT.md`/`docs/agents/`, and CI internals.

**Decision — the duplication is deliberate.** The same rule now lives twice: normative +
traceable in `shared/conventions.md` / the ADRs, and embedded as plain prose in each skill. That
is the price of a clean shipping surface, and it is cheap to police: when a convention changes,
grep the skills for the embedded phrasing and update them in the same PR.

**Decision — CI enforces the boundary.** `shared/scripts/lint_user_facing.py` (standard-library
only, fixture-tested like the frontmatter linter) scans `skills/` and `commands/` — including
non-markdown runtime assets — for forbidden references and fails the build on any hit. The rule
is codified as conventions §9 and a Definition of Done checkbox.

**What this does not change.** Contributor docs remain free to cite anything, including each
other and the skills. `README.md` at the repo root is the install/contribute surface, not a
runtime surface, and may keep dev instructions. Skills referencing each other (`bzm-pr-gate`
delegating to `bzm-baseline`) is product behavior, not leakage.
