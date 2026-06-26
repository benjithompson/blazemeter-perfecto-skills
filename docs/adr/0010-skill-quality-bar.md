# Skill quality bar: Definition of Done + thin CI, evals deferred

For v1, a skill is "done" when it passes a written **Definition of Done / conventions checklist**
(valid frontmatter; embeds Context Resolution; has a gotchas section and a fixed output template;
follows `shared/conventions.md`) and has been run once against a real BlazeMeter test before merge. A
**thin CI** check lints `SKILL.md` frontmatter and smoke-tests that shared scripts respond to
`--help`.

Behavioral skill evals (e.g. the skill-creator eval harness) are deliberately **deferred** to the
roadmap, to be added when the catalog grows or external contributions begin.

Rationale: for ~5 skills, eval infrastructure costs more than it returns; a documented DoD plus
structural CI gives most of the safety at a fraction of the maintenance. This deferral is explicit so
a future contributor doesn't assume evals were simply overlooked.
