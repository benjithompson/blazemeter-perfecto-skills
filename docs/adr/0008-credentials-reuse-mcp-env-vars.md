# Credentials: reuse the BlazeMeter MCP's env vars (configure once)

Shared scripts and any skill that calls the BlazeMeter REST API directly reuse the exact credential
scheme the BlazeMeter MCP already defines, so a user who has the MCP working needs no extra setup.

Resolution order:

1. `API_KEY_ID` + `API_KEY_SECRET` (env) — used if both are set
2. else `BLAZEMETER_API_KEY` (env) — a path to a JSON key file containing `{id, secret}`

No repo-specific config and no hardcoded default key-file path. The prior-art scripts' hardcoded
personal path (`/Users/ben.thompson/.../api-key.json`) is removed.

Rules: never commit keys (gitignore `api-key*.json`, `api-keys*.json`, `.env`); document the env vars
in the README; generated reports/artifacts must never embed credentials (e.g. `Authorization`
headers). The test-asset `auth.json` flow (`upload_assets`) is a separate concept from platform
credentials and keeps its own name.
