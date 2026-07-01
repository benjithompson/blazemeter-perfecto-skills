# BlazeMeter REST v4 — the endpoint contract `bzm_fetch.py` depends on

This is the distilled, load-bearing subset of the BlazeMeter API that the bulk-fetch
engine calls — **not** a copy of the swagger. Verified against the open-source
[bzm-mcp](https://github.com/Blazemeter/bzm-mcp) server source (the same endpoints the
MCP itself calls) and the [v4 explorer](https://a.blazemeter.com/api/v4/explorer/).
The fixtures under `tests/fixtures/bzm_fetch/` mirror these shapes; when BlazeMeter
drifts, the env-gated live tests (see `tests/test_bzm_fetch.py`) are the tripwire.

## Base URL & auth

- Base: `https://a.blazemeter.com/api/v4`
- Auth: **HTTP Basic** — `Authorization: Basic base64(key_id:key_secret)`.
- Credentials env vars (identical to the MCP's): `API_KEY_ID` + `API_KEY_SECRET`,
  else `BLAZEMETER_API_KEY` = **path** to a JSON key file `{"id": ..., "secret": ...}`.

## List envelope & pagination

Every list endpoint returns:

```json
{ "result": [...], "total": 123, "skip": 0, "limit": 50, "error": null }
```

Params: `limit` (documented max **50**), `skip` (offset), `sort[]=-updated`
(newest-first). `total` enables the one-request census used by `plan`.

## Endpoints used (all plain GETs)

| Purpose            | Endpoint                                        | Key params            |
|--------------------|-------------------------------------------------|-----------------------|
| Workspaces of acct | `/workspaces`                                   | `accountId`           |
| Projects of ws     | `/projects`                                     | `workspaceId`         |
| Tests of project   | `/tests`                                        | `projectId` (only — no workspaceId variant) |
| Project read       | `/projects/{id}`                                | —                     |
| Executions of test | `/masters`                                      | `testId`              |
| Executions in scope+window | `/masters`                              | `accountId` \| `workspaceId` \| `projectId`, `startTime`+`endTime` (epoch s) |
| Execution read     | `/masters/{id}`                                 | — (used by `run-pair`)|
| Summary report     | `/masters/{id}/reports/default/summary`         | —                     |
| Errors report      | `/masters/{id}/reports/errorsreport/data`       | —                     |
| Request stats      | `/masters/{id}/reports/aggregatereport/data`    | —                     |
| Anomaly stats      | `/masters/{id}/anomalies/stats`                 | — (**undocumented**)  |

## Field notes (the ones the engine reads)

- **Master (execution)**: `id`, `name`, `created`, `updated`, `ended` — **epoch
  seconds**; `ended == null` means still running. Verdict field is **`reportStatus`**
  ∈ `pass | fail | unset | abort | error | noData` (default `unset`). Archived flag:
  `dumped`. **List rows also carry `testId`, `projectId`, and `maxUsers`** (verified
  live), so a scope-wide listing needs no per-test iteration. **`/masters` accepts
  `startTime`/`endTime` (epoch seconds) for server-side window filtering** and can be
  scoped by `accountId`, `workspaceId`, or `projectId` — a whole-account 24h window is
  typically one request. Undocumented params tried and ignored by the API:
  `from/to`, `minCreated/maxCreated`, `createdAfter/createdBefore`. Caveat: the
  account-wide listing may return `total: "n/a"` — stop paging on a short page, not
  on `total`. The single read `/masters/{id}` returns the same shape plus `testId`
  (which the engine surfaces as `test_id` in `run-pair` output when present).
- **Summary**: `result.summary[]`; take the aggregate row where `id == "ALL"` (or
  `lb == "ALL"`). Fields: `hits`, `failed`, `avg`, `min`, `max`, `median`, `tp90`,
  `tp95`, `tp99`, `hits_avg` (throughput/s), `duration` (s), `maxUsers` (fallback
  `concurrency`), `bytes`, `size_avg`. Error % = `failed / hits`. **Caveat (seen
  live):** some run types (GUI/EUX) return the ALL row with every KPI `null` and
  `hits: null` even when `reportStatus` is `pass` — a zero-hit row means "no load
  KPIs", not a clean run, and some rows omit `tp95`/`tp99` keys entirely.
- **Aggregate (request stats)**: per-label rows: `labelId`, `labelName`, `samples`,
  `errorsCount`, `errorsRate` (unit undocumented — the engine derives the rate from
  `errorsCount / samples` instead), `avgResponseTime`, `90line`, `95line`, `99line`,
  `avgThroughput`, `concurrency`, `hasLabelPassedThresholds`.
- **Errors report**: per-label items: `labelId`, `name`, `errors[] {rc, m, count}`,
  `assertions[] {name, failureMessage, failures}`, `urls[] {url, count}`.
- **Anomaly stats**: `result.anomalyCount`, `result.anomalies[] {labelId, labelName,
  kpi, startTime, endTime, maxSpikeHeight}`. This endpoint is **not in the public API
  docs** (the MCP calls it via the same v4 base + Basic auth); the engine degrades
  gracefully — a failed/empty response is reported as `statistics_unavailable`,
  never treated as "no anomalies".
- **Test**: `id`, `name`, `projectId`; failure criteria live at
  `configuration.plugins.thresholds.thresholds[]` with `configuration.enableFailureCriteria`.
- **Account**: AI-consent flag is `aiConsent`. Consent gating is enforced
  client-side (the MCP refuses on `aiConsent != true`); the REST API itself does not
  block, which is why skills keep the consent gate in the MCP **before** invoking
  this engine.
