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
| Workspace read     | `/workspaces/{id}`                              | —                     |
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
| Labels of a master | `/data/labels`                                  | `master_id` (**undocumented**; `--timeseries`) |
| Intra-run series   | `/masters/{id}/kpi-values`                      | `id`, `interval` (`--timeseries`) |

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
- **Anomaly stats**: `result.anomalyCount`, `result.anomalies {labelId, labelName,
  kpi, startTime, endTime, maxSpikeHeight}`. **Shape caveat (seen live):**
  `anomalies` is an empty **list** when `anomalyCount` is 0 but an
  anomalyId-keyed **dict** of those rows when anomalies exist — the engine
  normalizes both (`anomaly_items`). This endpoint is **not in the public API
  docs** (the MCP calls it via the same v4 base + Basic auth); the engine degrades
  gracefully — a failed/empty response is reported as `statistics_unavailable`,
  never treated as "no anomalies".
- **Intra-run timeseries** (`--timeseries` on `history`/`run-pair`; the data behind
  the platform's live execution charts). **Status: verified against the official
  help-center "Time-series data" API docs; live verification is pending** — the
  `live`-marked pytest (`test_live_intra_run_timeseries_endpoints`) pins these
  assumptions and auto-runs when credentials are present.
  - `/masters/{id}/kpi-values` — the documented series endpoint. Both params
    required: `id` = `label/{labelId}/{kpi}/{statistic}` (slashes URL-encoded —
    `urlencode` already does this) and `interval` ∈ {1, 10, 60} seconds/bucket
    (docs recommend larger intervals to bound dataset size — the engine always
    uses **60**). Statistics per KPI: `t` → `avg,min,max,pec50,pec90,pec95,pec99`;
    `n,na,ec,lt,by` → `avg`; `rc/{code}` per response code. Datapoints carry `ts`
    (epoch **seconds**) plus the full multi-KPI field set (`n`, `na`, `ec`,
    `t_min/t_max/t_avg`, `t_pec50/90/95/99`, `lt_avg`, `by_avg`, `ct_avg`) —
    so ONE series request per run covers every curve the engine builds. The
    docs don't pin the datapoint container's field name — the engine finds the
    first list of `ts`-bearing dicts in each `result[]` entry. No pagination.
  - `/data/labels?master_id={id}` — flat label id/name list for a master
    (**undocumented**; verified by live route probing only). The engine matches
    the aggregate row named exactly `ALL`, tolerating both `id`/`name` and
    `labelId`/`labelName` conventions; whether every run type carries an ALL
    row is a live-verification unknown — absence degrades to
    `timeseries_unavailable`, never a guess at another label.
  - `/masters/{id}/reports/timeline/kpis` (KPI×label catalog tree) and
    `/api/v4/data/kpis` (multi-master overlay series, what Taurus polls live)
    exist but are **deliberately unused**: the flat label list is a simpler
    contract than the tree, and `data/kpis`'s `from`/`to` semantics on
    *completed* masters are unverified. Revisit `data/kpis` only if per-run
    `kpi-values` pulls ever become the cost driver.
- **Workspace**: `/workspaces/{id}` returns `id`, `name`, `accountId`, `enabled`
  (verified live). The sweep reads it once per **distinct active** workspace to
  name the v3 rollups; a failed read degrades to a null name (ids still group).
- **Test**: `id`, `name`, `projectId`; failure criteria live at
  `configuration.plugins.thresholds.thresholds[]` with `configuration.enableFailureCriteria`.
- **Account**: AI-consent flag is `aiConsent`. Consent gating is enforced
  client-side (the MCP refuses on `aiConsent != true`); the REST API itself does not
  block, which is why skills keep the consent gate in the MCP **before** invoking
  this engine.
