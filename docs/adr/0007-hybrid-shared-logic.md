# Hybrid sharing: centralized assets, self-contained skill prose

> **Status:** Partially superseded by [ADR-0014](0014-report-as-in-skill-model-filled-html-template.md)
> — the branded report template/renderer is **no longer** centralized in `shared/scripts/`; it ships
> as a static asset inside the `bzm-report` skill and is filled in-skill. The rest of this ADR
> (centralized `bzm-*` auth/artifact scripts, self-contained prose) still holds.

Skills share heavy, exact assets but keep their prose self-contained.

- **Centralized** (referenced via `${CLAUDE_PLUGIN_ROOT}`): deterministic scripts (the `bzm-*`
  auth/artifact utilities) and the branded report template/assets. No value in duplicating code or
  markup.
- **Self-contained**: each `SKILL.md` embeds its own account → workspace → project context-resolution
  step and reads top-to-bottom with no indirection. The house style is documented once in
  `shared/conventions.md` as the authoring standard that skills follow.

Why: skills stay readable and portable (a skill copied to `~/.claude/skills/` still reads correctly),
while we avoid duplicating real code.

Rejected: fully DRY prose (forces the model to read indirected files and breaks loose-copied skills)
and fully self-contained scripts (duplicated, drift-prone code).
