# Skills may call the BlazeMeter REST API directly when the MCP lacks a tool

Skills are not limited to the BlazeMeter MCP's tools. The MCP (source of truth: the
[`Blazemeter/bzm-mcp`](https://github.com/Blazemeter/bzm-mcp) repo) exposes a bounded set of
actions; when a needed capability isn't among them, a skill may call the BlazeMeter REST API v4
(`https://a.blazemeter.com/api/v4`, explorer at <https://a.blazemeter.com/api/v4/explorer/>)
directly.

Principle: **MCP-first, API-fallback.** Prefer an MCP tool when one exists — it is mediated, auth
is already configured, and the call is safer and more stable. Drop to the raw API only to reach
capabilities the MCP doesn't cover (e.g. test-data management, scheduling, notifications, and — in
the current MCP — Service Virtualization and API Monitoring, which are absent).

Trade-off accepted: API-using skills must handle credentials themselves and are coupled to the
API's shape, so they are more brittle than MCP-mediated ones. We pay that cost only where it buys
capability we otherwise couldn't reach.

The authoritative MCP action surface was read at repo ref `Blazemeter/bzm-mcp@9a69eab` and must be
re-verified against the source as the MCP evolves — never reasoned about from memory.
