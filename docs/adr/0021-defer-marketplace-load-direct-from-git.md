# Defer the marketplace; load direct from a git checkout during active development

> **Note:** originally filed as ADR-0014; renumbered to 0021 to resolve a number collision with
> [0014-report-as-in-skill-model-filled-html-template.md](0014-report-as-in-skill-model-filled-html-template.md).

**Amends [ADR-0006](0006-distribution-plugin-marketplace.md).** While the plugin is still early and
changing fast, it is consumed as a **skills-directory plugin** loaded **in place** from a local git
checkout — symlink the repo into `~/.claude/skills/` and Claude Code discovers it as
`perforce@skills-dir`. Publishing through the self-hosted marketplace is **deferred**
until the plugin is built out further; `.claude-plugin/marketplace.json` is kept ready for that.

Why: a *marketplace* install is **version-pinned** — Claude Code clones the marketplace repo, copies
the plugin into a cache, and only updates when the plugin `version` increases. In rapid development
that produces stale-install pain: a maintainer edits skills, forgets to bump `version` (or doesn't
refresh the marketplace clone), and installs silently keep serving the old build. (This bit us: four
skills stayed invisible at a pinned `0.1.0`, and even after the bump the local marketplace clone
needed a separate `/plugin marketplace update`.) Loading direct from the checkout removes the pin
entirely: `git pull` + `/reload-plugins` is the whole update loop, `SKILL.md` edits are live, and
there is no version to bump for day-to-day work. The location is shared across the CLI, the VS Code
extension, and the desktop app, so one symlink covers all three.

Tradeoff (this reverses ADR-0006's "symlink rejected as clunky for individual users"): the symlink
is per-user setup, not a checked-in shared artifact, so it suits the **maintainer/dev** loop, not
broad distribution. That is acceptable now — the audience for an unfinished plugin is essentially
its developers. When the plugin matures and we want one-step install for outside users, revisit
ADR-0006 and re-enable the marketplace path (bump `version`, publish), at which point the
version-pin discipline (DoD checkbox) applies again.

Unchanged from ADR-0006: it's still a namespaced Claude Code plugin (`perforce:<skill>`),
still skill-centric (ADR-0002), still works across all Claude Code surfaces.
