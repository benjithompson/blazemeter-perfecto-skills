# Skills use the MCP first, but may call the BlazeMeter REST API directly

Skills are not limited to the operations the BlazeMeter MCP exposes. The MCP exposes a bounded set of
actions; when a needed capability isn't among them, a skill may call the BlazeMeter REST API (v4)
directly.

**Sources of truth** (reason from these, not from memory):

- MCP capabilities: the bzm-mcp repo — https://github.com/Blazemeter/bzm-mcp (the action surface was
  last read at ref `Blazemeter/bzm-mcp@9a69eab`; re-verify against source as the MCP evolves).
- BlazeMeter REST API reference: the v4 explorer — https://a.blazemeter.com/api/v4/explorer/
  (base `https://a.blazemeter.com/api/v4`).

**Preference order — MCP-first, API-fallback:** use the MCP tool when one exists (it's higher-level,
auth is already configured, and the call is safer and more stable). Drop to the raw API only to reach
capabilities the MCP doesn't cover — e.g. test-data management, scheduling, notifications, and (in the
current MCP) Service Virtualization and API Monitoring, which are absent.

**Trade-off accepted:** API-using skills handle credentials themselves (see ADR-0008) and are coupled
to the API's shape, so they're more brittle than MCP-mediated ones. We pay that cost only where it
buys capability we otherwise couldn't reach. A skill that uses the API directly should say so and note
why the MCP didn't suffice.
