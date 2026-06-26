# BlazeMeter & Perfecto Skills

A shared library of Claude Code skills and commands that help users get more value from Perforce's
BlazeMeter and Perfecto testing platforms via their MCP servers.

## Language

**Platform**:
One of the Perforce testing products this repo targets — currently BlazeMeter or Perfecto —
accessed through its MCP server.
_Avoid_: tool, service, product

**Platform Customer**:
A QA or performance engineer who has their own BlazeMeter/Perfecto account and uses these skills to
work faster. The primary audience.
_Avoid_: end user, client

**Field Team**:
Perforce-internal roles (sales engineering, support, customer success) who use these skills on a
customer's behalf or to demo and onboard. The secondary audience.
_Avoid_: internal user, staff

**Expert-Workflow Skill**:
The primary unit of this repo: a skill that encodes one opinionated, expert-judgment task on top of
raw MCP calls — chained calls, gotchas, and a fixed output shape. The durable value the MCP itself
lacks.
_Avoid_: wrapper, helper

**Journey**:
A larger skill that orchestrates a multi-step lifecycle, sometimes spanning both platforms. Used
sparingly for flagship stories, not as the default skill shape.
_Avoid_: pipeline, flow

**Command**:
A slash-command that exists only as a convenience entry point to a skill. Not a standalone unit of
value.
_Avoid_: wrapper, shortcut

**Pillar**:
A top-level capability area we build a vertical of skills around. The four named pillars are
BlazeMeter Performance, BlazeMeter Virtual Services, BlazeMeter API Test & Monitoring, and Perfecto.
v1 covers only the first.
_Avoid_: area, module

**Report**:
A generated, shareable artifact (typically branded HTML) that combines BlazeMeter data across tests,
executions, environments, or time — the cross-cutting views the platform itself can't produce.
Distinct from a single-execution platform report.
_Avoid_: export, dashboard

**Branded Report Template**:
The deterministic, BlazeMeter-styled HTML/CSS shell (layout, chrome, chart scaffolding) that a Report
fills with AI-generated data and narrative. The source of a Report's determinism and on-brand
consistency.
_Avoid_: theme, skin

**Context Resolution**:
The standard opening step of a skill that resolves and displays the full account → workspace →
project (→ test) chain before acting, so every skill operates against a verified context. Defined
once in `shared/conventions.md` and embedded in each skill.
_Avoid_: lookup, setup

**Platform Credentials**:
The BlazeMeter API key (`{id, secret}`) used to authenticate to the BlazeMeter platform, sourced from
the MCP's env vars (`API_KEY_ID`/`API_KEY_SECRET`, or `BLAZEMETER_API_KEY` pointing to a key file).
Distinct from a test's asset `auth.json`.
_Avoid_: auth (when you mean platform login), key file

**Brand Config**:
A small, swappable set of brand values (colors, logo, fonts) the Branded Report Template reads. Ships
with approximated BlazeMeter branding in v1; swapping to official assets is a config edit, not a
template change.
_Avoid_: theme file
