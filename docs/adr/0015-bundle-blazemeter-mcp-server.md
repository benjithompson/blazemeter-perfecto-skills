# Bundle the BlazeMeter MCP server in the plugin (`.mcp.json`), env-var placeholders only

The plugin ships a root **`.mcp.json`** that defines the **BlazeMeter** MCP server, so enabling the
plugin auto-connects it — a fresh install gets working tools without a separate manual MCP setup.
The definition mirrors the proven `uvx` invocation:

```json
{ "mcpServers": { "BlazeMeter-MCP": {
  "type": "stdio", "command": "uvx",
  "args": ["--from", "git+https://github.com/Blazemeter/bzm-mcp.git@v1.2.0", "-q", "bzm-mcp", "--mcp"],
  "env": { "BLAZEMETER_API_KEY": "${BLAZEMETER_API_KEY}" } } } }
```

**No secrets or machine paths committed.** Only the `${BLAZEMETER_API_KEY}` placeholder ships;
Claude Code expands `${VAR}` from the user's environment at launch. This upholds conventions §6 and
ADR-0008 (credentials live in the environment, never in the repo). `uvx` self-fetches bzm-mcp at the
pinned `v1.2.0`, so the only host requirement is `uv`/`uvx` on `PATH`.

**Why this is safe alongside an existing manual config.** Claude Code dedupes MCP servers; a
**plugin** server is matched **by endpoint** (its command), and when the same server exists at a
higher scope (e.g. a user-scope `~/.claude.json` entry) Claude Code connects **once** using the
higher-precedence definition. So a maintainer who already configured BlazeMeter MCP keeps their
config; the bundled entry only fills the gap for those who haven't. There is no duplicate-tool risk.

**Credentials form.** The bundled `uvx` server reads `BLAZEMETER_API_KEY` (a path to the JSON key
file), per bzm-mcp's binary method. bzm-mcp's `API_KEY_ID` + `API_KEY_SECRET` pair is its *Docker*
method and is intentionally not used here.

**Desktop caveat.** The desktop app inherits only `PATH` (+ a few Claude vars) from the shell
profile, so users set `BLAZEMETER_API_KEY` via **Settings → Claude Code → local environment
editor**; the bundled server reads it from there. (See ADR-0014 for the skills-dir load model —
`.mcp.json` changes need `/reload-plugins`.)

**Scope: BlazeMeter only.** Perfecto MCP is **not** bundled. The plugin ships no Perfecto skills, and
Perfecto's local binary path is machine-specific (no portable default), so bundling it would only
add a failing-server risk. Revisit when Perfecto skills land — likely via a `${PERFECTO_MCP_PATH}`
placeholder mirroring this pattern.
