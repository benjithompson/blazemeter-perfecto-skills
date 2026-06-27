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
| `blazemeter-report` | Generates a branded, self-contained HTML cross-run trend & regression Report over a time window — trend charts, regression flags, and SLA compliance across many runs, rendered offline from a shipped HTML template the skill fills in (no local interpreter needed). |

Each skill is **also a slash command**: once the plugin is installed, every skill appears in the
`/` menu (namespaced) as **`/blazemeter-perfecto:<skill-name>`**, e.g.
`/blazemeter-perfecto:run-blazemeter-test`. You don't need separate command wrappers — the skill
*is* the command, and Claude can also invoke it automatically when it's relevant.

## Prerequisites

1. **Claude Code** — the CLI, the VS Code extension, or the desktop app (all supported).
2. **The BlazeMeter MCP server.** The plugin **bundles** its definition (`.mcp.json`), so enabling
   the plugin auto-connects [bzm-mcp](https://github.com/Blazemeter/bzm-mcp) — you just need
   [`uv`/`uvx`](https://docs.astral.sh/uv/) on your `PATH` (the bundled server launches it via
   `uvx`). Already have a BlazeMeter MCP configured manually? No conflict — Claude Code dedupes by
   endpoint and your existing config (higher precedence) wins.
3. **BlazeMeter API credentials** (see below). The bundled server and the skills both read them from
   the environment — one setup.

## Install

While the plugin is under active development it loads **directly from a local git checkout** — no
marketplace, no install step, no version pin. Clone the repo, then symlink it into your personal
skills directory so Claude Code discovers it as a plugin:

```bash
git clone https://github.com/benjithompson/blazemeter-perfecto-skills.git
ln -s "$(pwd)/blazemeter-perfecto-skills" ~/.claude/skills/blazemeter-perfecto
```

Any folder under `~/.claude/skills/` that contains a `.claude-plugin/plugin.json` loads
automatically on the next session as **`blazemeter-perfecto@skills-dir`** — the skills then appear
(namespaced) in the `/` menu on the **CLI, the VS Code extension, and the desktop app** (the
`~/.claude/skills/` location is shared across all three). No `/plugin install` needed.

> Don't want a symlink? Clone directly into `~/.claude/skills/blazemeter-perfecto` instead. Or, for
> a one-off session, launch the CLI with `claude --plugin-dir /path/to/blazemeter-perfecto-skills`.

### Updating

Because the plugin is read **in place** from your checkout, updating is just:

```bash
git -C ~/.claude/skills/blazemeter-perfecto pull
```

Then `/reload-plugins` (or start a new session). Edits to a `SKILL.md` take effect immediately;
changes to other components (`.mcp.json`, `hooks/`, etc.) need the reload. **No version bump or
reinstall required** — that's the payoff of loading direct from git.

To stop loading it, remove the symlink (`rm ~/.claude/skills/blazemeter-perfecto`) or run
`claude plugin disable blazemeter-perfecto@skills-dir`.

> **Marketplace distribution is deferred.** Once the plugin is built out further it will be
> published via a self-hosted marketplace (`.claude-plugin/marketplace.json` is kept ready for
> that). Until then, use the direct-from-git setup above.

### On the Claude Code desktop app

Skills-directory plugins, namespaced skills/commands, hooks, and MCP servers are all fully supported
in the desktop app's **local** and **SSH** sessions (they are *not* available in cloud sessions).
The `~/.claude/skills/` symlink above is picked up by the desktop app the same as the CLI. Two
desktop-specific notes:

- **The BlazeMeter MCP server is bundled.** The plugin's `.mcp.json` auto-connects it when the
  plugin is enabled — no manual setup — as long as `uvx` resolves on the local-session `PATH` (the
  server launches via `uvx`). An already-configured BlazeMeter MCP is deduped by endpoint, so your
  existing setup wins. *(Perfecto MCP is not bundled yet — there are no Perfecto skills here; add it
  manually if you need it.)*
- **Environment variables: set them in the desktop env editor.** The desktop app inherits only
  `PATH` (plus a few Claude variables) from your shell profile — it does **not** pick up other
  `export`ed vars. So set your BlazeMeter credentials (`API_KEY_ID` + `API_KEY_SECRET`, or
  `BLAZEMETER_API_KEY`) via **Settings → Claude Code → local environment editor** (or the
  environment dropdown in the prompt box → **Local** → gear icon) — the bundled MCP server reads
  them from there too. No language runtime is required: every skill (including `blazemeter-report`,
  which fills a shipped HTML template) runs with just the MCP and credentials — nothing is shelled
  out to a local interpreter.

## Credentials

Skills reuse the **same environment variables the BlazeMeter MCP uses** — configure them once for
the MCP and you're done. Precedence:

1. `API_KEY_ID` + `API_KEY_SECRET` — your BlazeMeter API key id and secret (preferred, used only
   when **both** are set); else
2. `BLAZEMETER_API_KEY` — a path to a BlazeMeter API key file: JSON of the shape
   `{ "id": "...", "secret": "..." }`.

> The **bundled MCP server** (`.mcp.json`) launches bzm-mcp via `uvx`, which reads
> **`BLAZEMETER_API_KEY`** (the key-file path) — set that env var and you're done. (The
> `API_KEY_ID` + `API_KEY_SECRET` pair is bzm-mcp's *Docker* method; the `uvx` invocation we bundle
> uses the key-file form.) The `.mcp.json` ships only a `${BLAZEMETER_API_KEY}` placeholder — never
> a key or a path.

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
