# Bulk BlazeMeter data pulls run in deterministic scripts; the MCP stays control-plane

The cross-test skills (`bzm-daily-digest`, and next `bzm-portfolio-report`,
`bzm-analyze-test`, `bzm-report`, `bzm-compare-runs`) were specified as chains of
`blazemeter_*` MCP calls. At real account sizes that is untenable: a whole-account
digest is workspaces × projects × tests × executions × three sub-reports — easily
thousands of MCP payloads at 1–3 KB each, i.e. **millions of tokens and long
wall-clock**, all for data the model immediately reduces to one scoreboard row per
test. Even a ten-test project costs ~100k tokens of raw KPI blobs, and the ≥10%
delta arithmetic ends up done probabilistically by the model.

**Decision — the line is structural, not numeric.** Decidable *before the first
call*, from the shape of the operation alone:

- **MCP (control plane; the AI reads the payload):** scope resolution and the
  interactive picks (user/account/workspace/project reads and one-page lists), the
  AI-consent gate, anything that mutates, and **single-object drill-ins** after the
  digest (one execution's reports when the user asks about *that* run).
- **Script (data plane; the AI never sees raw payloads):** any **data-driven
  fan-out** — "for each X, list/read Y" where the iteration count comes from data,
  not from the user's pick. There is no "small account, MCP is fine" branch: one
  code path, deterministic and testable, even when N is small.

**Decision — one shared engine, subcommands by fetch pattern.**
`shared/scripts/bzm_fetch.py` holds auth, pagination, retry/backoff, and coverage
accounting once. Subcommands land with their first consumer: `plan` (scope census
for the "narrow or proceed?" checkpoint) and `sweep` (windowed cross-test digest)
ship with `bzm-daily-digest`; `history` (one test's run history) and `run-pair`
(two executions) follow with the skills that need them. No per-skill copies.

**Decision — aggregation lives inside the script.** The engine does everything
deterministic: window filtering, status buckets (only `pass`/`fail` runs roll into
KPIs; `abort`/`error` are counted as skipped-partial, `unset`/`noData` as
inconclusive), per-test baseline resolution (conversational pins > committed
`.blazemeter/baseline.json` > last-passing, reusing `bzm_baseline` as a module),
the ≥10% adverse-move rule, RPS-per-VU normalization when the load config changed,
the baseline-is-the-only-run guard, and incident-candidate extraction. It emits ONE
digest JSON of size **O(tests)** (`schema_version` field guards the contract) plus a
five-line stdout summary. The model contributes only judgment: severity ranking,
narrative, the markdown.

**Decision — runtime posture.** Same env vars as the MCP (`API_KEY_ID` +
`API_KEY_SECRET`, else `BLAZEMETER_API_KEY` as a key-file path); secrets never on
argv, never echoed. Bounded parallelism (default 8, `--concurrency`). On 429/5xx:
backoff-and-continue (per-item failures recorded, never aborting the sweep); the
digest's `coverage` block reports attempted/failed fetches so a partial sweep is
never presented as complete. `--max-failure-rate` (default 20%) exits non-zero so a
scheduled digest alarms instead of publishing garbage. Only scope-level failures
(bad credentials, root listing fails) hard-fail.

**Decision — API truth and drift.** The endpoint contract is distilled into
`shared/scripts/API_NOTES.md` from the open-source bzm-mcp server (what the MCP
actually calls) cross-checked with the v4 explorer — the full swagger is not
vendored and no client is generated (stdlib-only stands). Fixtures under
`tests/fixtures/bzm_fetch/` mirror those shapes; env-gated live tests (auto-run
when credentials are present, auto-skip in CI, marker `live`) catch drift at
minimal scope. The undocumented `/masters/{id}/anomalies/stats` endpoint degrades
gracefully to `statistics_unavailable`.

**Amendment (2026-07-01) — window-first sweeps.** The original `sweep` enumerated the
test catalog (workspaces × projects × tests) and listed executions per test — O(catalog)
even when almost nothing ran. `/masters` turns out to accept server-side
`startTime`/`endTime` filtering scoped by `accountId`/`workspaceId`/`projectId`, and its
list rows carry `testId`/`projectId`/`maxUsers` — so `sweep` and `plan` are now
**window-first**: one filtered listing finds every in-window run across the scope
(whole-account 24h ≈ one request), grouped by `testId`; baselines and reports are
fetched only for **active** tests. Cost scales with activity, not catalog size
(SE Demo: 166 workspaces / 6,339 tests → seconds instead of minutes). The digest schema
(v2; since advanced to v3 — workspace names, per-test health, workspace/project rollups) drops `tests_in_scope`/`idle_tests` — unknowable without a catalog walk — in favor
of `runs_in_window`; `plan` is now a window census (runs/tests active), which is the
sweep's true cost driver and the right practicality guard.

**Amendment (2026-07-02) — the census moves back to the MCP; `plan` retired.** bzm-mcp
v1.3.0 adds account-wide `search` actions to `blazemeter_tests` and
`blazemeter_execution` (POST `/search`, 50/page with `page_index`, `total` in the
envelope; filters include `workspace_id_list`, `project_id_list`, name `$ilike`, and a
time frame — note filtering is day-granular: preset starts snap to midnight, and `custom`
windows snap both bounds to midnight with the end day exclusive, so callers pass `end_time`
as the day after the window end). That covers exactly the
control-plane discovery slice of this ADR: the window census (`total` = runs-in-window
in one call) and name→id resolution without paging. The `plan` subcommand is deleted;
skills census via `blazemeter_execution search` in Step 0 instead. **The data-plane
line is unchanged**: search rows are discovery metadata only — an execution row carries
no `testId`, no pass/fail status, and no KPIs, and the MCP still exposes no
summary/request-stats/anomaly/kpi-values report reads — so `sweep`, `history`, and
`run-pair` (and all deterministic aggregation) stay in the engine.

**What this does not change.** Step 0 Context Resolution stays MCP and interactive
(including the consent gate — enforced client-side, so it must run *before* the
engine). Drill-ins stay MCP. The conventions' "MCP-first" posture survives as
"MCP-first for the control plane"; REST here is not the old documented-gap
fallback but the standing rule for bulk data-plane reads (conventions §5).
