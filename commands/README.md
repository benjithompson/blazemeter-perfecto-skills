# Commands

Thin command entry points to skills live here. Per the PRD (issue #1), commands exist **only** as
thin entry points to skills — there is no wrapper-per-MCP-operation. A command is a small `*.md`
file that invokes a skill; the expertise stays in the skill under `skills/`.

v1 ships its capability as a skill, invoked namespaced as
`blazemeter-perfecto:analyze-blazemeter-test` (skills are auto-discovered from `skills/` and the
model can invoke them directly), so no command wrapper is required yet. Add one here only when a
short, memorable `/command` entry point genuinely helps.
