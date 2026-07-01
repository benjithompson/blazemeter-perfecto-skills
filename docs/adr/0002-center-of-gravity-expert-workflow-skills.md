# Center of gravity: a toolbox of expert-workflow skills

The repo is built primarily as independent **expert-workflow skills** — each encoding one
opinionated, expert-judgment task on top of raw MCP calls (chained calls, gotchas, fixed output
templates), in the mould of `bzm-analyze-test`. The durable value is *encoded expertise* the
MCP servers don't have.

Rejected as the center of gravity:

- **Thin command wrappers** over single MCP operations — the MCP servers already expose these
  directly and even ship their own reference content, so wrappers add little durable value.
- **Large end-to-end journeys** as the default — used sparingly for a few flagship cross-platform
  stories, not as the standard unit.

Commands exist only as convenience entry points that invoke a skill, not as standalone value. A
reader won't find a wrapper for every MCP action — that omission is deliberate.
