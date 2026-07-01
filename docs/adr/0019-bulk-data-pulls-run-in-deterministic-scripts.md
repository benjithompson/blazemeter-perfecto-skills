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

**What this does not change.** Step 0 Context Resolution stays MCP and interactive
(including the consent gate — enforced client-side, so it must run *before* the
engine). Drill-ins stay MCP. The conventions' "MCP-first" posture survives as
"MCP-first for the control plane"; REST here is not the old documented-gap
fallback but the standing rule for bulk data-plane reads (conventions §5).
