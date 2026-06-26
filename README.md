# BlazeMeter & Perfecto Skills

> A Claude Code **plugin** of expert-workflow skills that turn the BlazeMeter MCP into
> performance-engineering judgment — not just raw operations.

The BlazeMeter MCP can fetch a single execution's numbers. It won't tell you whether a build
regressed, why a run failed, which endpoint is the culprit, or how a service trended over a
quarter. This plugin encodes that judgment as installable skills that work in **both** the Claude
Code CLI and the VS Code extension, reusing the credentials you already set for the MCP.

v1 goes depth-first on the BlazeMeter **Performance** pillar. More of the lifecycle (run, compare,
triage) and a branded cross-run report engine are planned — see the [PRD](../../issues/1) and open
issues.

## What's included (v1)

| Skill | What it does |
| --- | --- |
| `analyze-blazemeter-test` | Analyzes a test's full execution history — response-time trends, regressions, tail-latency, error patterns, anomalies, per-endpoint hot spots, and SLA/failure-criteria compliance — and delivers a QA performance assessment. |

Invoked (namespaced) as **`blazemeter-perfecto:analyze-blazemeter-test`**.

## Prerequisites

1. **Claude Code** (CLI or the VS Code extension).
2. **The BlazeMeter MCP server**, installed and connected to Claude Code. It is the source of
   truth for capabilities — see [bzm-mcp](https://github.com/Blazemeter/bzm-mcp).
3. **BlazeMeter API credentials configured for the MCP** (see below). These skills reuse them — no
   second setup.

## Install

The plugin lives in a self-hosted marketplace (this repo). Add the marketplace once, then install
the plugin. The **same two commands work in the CLI and in the VS Code extension** (run them in
the Claude Code prompt), and the skills then appear in both surfaces:

```text
/plugin marketplace add benjithompson/blazemeter-perfecto-skills
/plugin install blazemeter-perfecto@blazemeter-perfecto-skills
```

- `/plugin marketplace add` takes this GitHub `owner/repo` (it reads
  `.claude-plugin/marketplace.json`).
- `/plugin install <plugin>@<marketplace>` installs the `blazemeter-perfecto` plugin from the
  `blazemeter-perfecto-skills` marketplace.

> Prefer not to use the marketplace? The `skills/` folders are plain, copy-able skills — drop one
> into your `~/.claude/skills/` (unnamespaced) as a fallback.

## Credentials

Skills reuse the **same environment variables the BlazeMeter MCP uses** — configure them once for
the MCP and you're done. Precedence:

1. `API_KEY_ID` + `API_KEY_SECRET` — your BlazeMeter API key id and secret (preferred); else
2. `BLAZEMETER_API_KEY` — a path to a BlazeMeter API key file.

Keys are **never committed** to a repo and **never embedded** in generated reports. A test's asset
`auth.json` (which authenticates the system under test) is a separate thing from these platform
credentials.

## Use it

In the CLI or VS Code, ask Claude to analyze a test, or invoke the skill directly:

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
