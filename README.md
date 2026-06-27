# BlazeMeter & Perfecto Skills

> A Claude Code **plugin** of expert-workflow skills that turn the BlazeMeter MCP into
> performance-engineering judgment — not just raw operations.

The BlazeMeter MCP can fetch a single execution's numbers. It won't tell you whether a build
regressed, why a run failed, which endpoint is the culprit, or how a service trended over a
quarter. This plugin encodes that judgment as installable skills that work across **all** Claude
Code surfaces — the CLI, the VS Code extension, and the desktop app — reusing the credentials you
already set for the MCP.

The plugin goes depth-first on the BlazeMeter **Performance** pillar — covering the lifecycle from
run → analyze → compare → triage → report. Later pillars (Perfecto, Virtual Services, API
Monitoring) are planned — see the [PRD](../../issues/1) and open issues.

## What's included

| Skill | What it does |
| --- | --- |
| `analyze-blazemeter-test` | Analyzes a test's full execution history — response-time trends, regressions, tail-latency, error patterns, anomalies, per-endpoint hot spots, and SLA/failure-criteria compliance — and delivers a QA performance assessment. |
| `run-blazemeter-test` | Runs a test end-to-end — optionally sets a simple load profile (with confirmation), starts the execution, polls to completion, and reports a pass/fail summary against the test's failure criteria. |
| `compare-blazemeter-runs` | Compares two executions (baseline vs candidate) — diffs response-time percentiles, throughput, and error rate with magnitude and direction, flags regressions past a threshold, and emits a ship / no-ship verdict. |
| `triage-blazemeter-failure` | Deep-dives one failed or regressed run — breaks errors down by type and endpoint, ranks endpoint hot spots, summarizes anomalies, separates systemic problems from noise, and ends with prioritized next steps. |
| `blazemeter-report` | Generates a branded, self-contained HTML cross-run trend & regression Report over a time window — trend charts, regression flags, and SLA compliance across many runs, rendered offline via the report engine. |

Each skill is **also a slash command**: once the plugin is installed, every skill appears in the
`/` menu (namespaced) as **`/blazemeter-perfecto:<skill-name>`**, e.g.
`/blazemeter-perfecto:run-blazemeter-test`. You don't need separate command wrappers — the skill
*is* the command, and Claude can also invoke it automatically when it's relevant.

## Prerequisites

1. **Claude Code** — the CLI, the VS Code extension, or the desktop app (all supported).
2. **The BlazeMeter MCP server**, installed and connected to Claude Code. It is the source of
   truth for capabilities — see [bzm-mcp](https://github.com/Blazemeter/bzm-mcp).
3. **BlazeMeter API credentials configured for the MCP** (see below). These skills reuse them — no
   second setup.

## Install

The plugin lives in a self-hosted marketplace (this repo). Add the marketplace once, then install
the plugin. The **same two commands work in the CLI, the VS Code extension, and the desktop app**
(run them in the Claude Code prompt), and the skills then appear in every surface:

```text
/plugin marketplace add benjithompson/blazemeter-perfecto-skills
/plugin install blazemeter-perfecto@blazemeter-perfecto-skills
```

- `/plugin marketplace add` takes this GitHub `owner/repo` (it reads
  `.claude-plugin/marketplace.json`).
- `/plugin install <plugin>@<marketplace>` installs the `blazemeter-perfecto` plugin from the
  `blazemeter-perfecto-skills` marketplace.

After installing (or updating), run `/reload-plugins` (or restart Claude Code) so the new skills
load. The plugin is **version-pinned** — installs only pick up new skills when the plugin's
`version` is bumped, so if you previously installed an older version and don't see all five skills,
reinstall or update.

> Prefer not to use the marketplace? The `skills/` folders are plain, copy-able skills — drop one
> into your `~/.claude/skills/` (unnamespaced) as a fallback.

### On the Claude Code desktop app

Plugins, marketplaces, namespaced skills/commands, hooks, and MCP servers are all fully supported
in the desktop app's **local** and **SSH** sessions (they are *not* available in cloud sessions).
Install exactly as above. Two desktop-specific notes:

- **Configure the BlazeMeter (and Perfecto) MCP server for local sessions.** This plugin reuses the
  MCP — it does not bundle it — so the MCP server must be connected in the desktop app just like in
  the CLI (via `~/.claude.json` / `.mcp.json`, which desktop and CLI share, or the **+ →
  Connectors** flow).
- **Environment variables: set them in the desktop env editor.** The desktop app inherits only
  `PATH` (plus a few Claude variables) from your shell profile — it does **not** pick up other
  `export`ed vars. So set your BlazeMeter credentials (`API_KEY_ID` + `API_KEY_SECRET`, or
  `BLAZEMETER_API_KEY`) via **Settings → Claude Code → local environment editor** (or the
  environment dropdown in the prompt box → **Local** → gear icon). The `blazemeter-report` skill
  also shells out to a `python` interpreter, so ensure one resolves on the local-session `PATH`.

## Credentials

Skills reuse the **same environment variables the BlazeMeter MCP uses** — configure them once for
the MCP and you're done. Precedence:

1. `API_KEY_ID` + `API_KEY_SECRET` — your BlazeMeter API key id and secret (preferred, used only
   when **both** are set); else
2. `BLAZEMETER_API_KEY` — a path to a BlazeMeter API key file: JSON of the shape
   `{ "id": "...", "secret": "..." }`.

Keys are **never committed** to a repo (key files like `api-key*.json` are gitignored) and **never
embedded** in generated reports. A test's asset `auth.json` (which authenticates the system under
test) is a separate thing from these platform credentials.

## Use it

In the CLI, VS Code, or the desktop app, ask Claude to analyze a test, or invoke the skill directly
from the `/` menu:

```text
> Analyze my BlazeMeter test "Checkout API – Peak" and tell me if it's regressing.
```

The skill first **resolves and shows you the account → workspace → project → test it's operating
on**, then produces the trend / regression assessment. If you give only a test name, it resolves
it within your default project; if it can't resolve the context, it stops and tells you rather than
guessing.

## Contributing

See [CONTRIBUTING.md](./CONTRIBUTING.md) and the house style in
[`shared/conventions.md`](./shared/conventions.md). Work is tracked in
[Issues](../../issues); grab one labelled `ready-for-agent` or `ready-for-human`.

## License

[Apache-2.0](./LICENSE).
