# Commands

Thin command entry points to skills live here. Per the PRD (issue #1), commands exist **only** as
thin entry points to skills — there is no wrapper-per-MCP-operation. A command is a small `*.md`
file that invokes a skill; the expertise stays in the skill under `skills/`.

Every skill is **already a slash command**: Claude Code surfaces each `skills/<name>/SKILL.md` in
the `/` menu as `/blaze:<name>` (on the CLI, VS Code, and the desktop app alike), and
if a command and a skill share a name the **skill takes precedence**. So a per-skill wrapper here
would be redundant (and ignored), and the `blaze:` namespace applies regardless — a
wrapper can't even shorten the invocation.

Add a command here only when a genuinely *different* entry point helps (e.g. a composite workflow
that chains several skills), not as a 1:1 alias for an existing skill. The expertise stays in the
skill under `skills/`.
