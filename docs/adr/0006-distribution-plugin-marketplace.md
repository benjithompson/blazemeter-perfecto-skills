# Distribution: a Claude Code plugin published via a marketplace

The repo is distributed as a **Claude Code plugin** (with a `.claude-plugin/` manifest) published
through a **marketplace**, so users install once and use it in both the Claude Code CLI and the
VS Code extension — both surfaces share the same config and plugin directories.

Why: one artifact covers both surfaces; plugins are versioned and updatable; plugin skills are
namespaced (e.g. `blazemeter-perfecto:analyze-blazemeter-test`), avoiding collisions in a shared
install. The loose skill folders remain copy-able into `~/.claude/skills/` as a no-machinery
fallback.

Rejected: manual copy as the primary path (unversioned, error-prone) and git submodule/symlink
(clunky for individual users).

Note: VS Code shows only a *subset* of typed slash-commands in its picker, but model-invoked skills
trigger in both surfaces — which reinforces the skill-centric center of gravity (ADR-0002).
