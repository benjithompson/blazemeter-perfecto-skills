# Skills use the MCP first, but may call the BlazeMeter REST API directly

Skills are not limited to the operations a platform's MCP server exposes. When the MCP lacks a
needed action, a skill may call the BlazeMeter REST API (v4) directly.

**Sources of truth** (reason from these, not from memory):

- MCP capabilities: the bzm-mcp repo — https://github.com/Blazemeter/bzm-mcp
- BlazeMeter REST API reference (explorer): https://a.blazemeter.com/api/v4/explorer/

**Preference order:** use the MCP tool when one exists (it's higher-level and handles
account/workspace/project context); fall back to the REST API only to fill genuine gaps. Direct API
calls carry their own auth and maintenance cost and may break if the API changes, so prefer the MCP
where both would work. A skill that uses the API directly should say so and note why the MCP didn't
suffice.
