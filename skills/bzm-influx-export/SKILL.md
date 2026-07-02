---
name: bzm-influx-export
description: Export BlazeMeter run history — per-run KPI summaries plus minute-by-minute intra-run curves — from an account, workspace, or project into an InfluxDB 2.x bucket, either as a one-shot backfill (default last 7 days) or an incremental watermark-based sync, and hand the user ready-made Flux queries and a cron line for continuous ingestion. Use when asked to export/push/stream BlazeMeter results into InfluxDB (or a Grafana-backed time-series store), to backfill a metrics database with run history, to set up recurring/nightly sync of load-test results, or for long-horizon trending and cross-project dashboards that BlazeMeter's own UI doesn't offer.
---

Export BlazeMeter execution results into **InfluxDB 2.x** so the user can build long-horizon per-test trends and workspace/project rollup dashboards outside BlazeMeter. Each ended run becomes one `bzm_run` point (summary KPIs, stamped at the run's **end** time) and — unless curves are skipped — a series of `bzm_run_point` points (one per minute bucket of the run's intra-run KPI curves). Where `bzm-daily-digest` judges a window and moves on, this skill makes the raw history **durable and queryable**: backfill once, then sync incrementally forever. Sub-minute data is explicitly not a goal.

**Division of labor (important):** the MCP is used for the *control plane* — resolving the account/scope interactively, the AI-consent gate, and the window census. The *bulk pull and export* (every ended run's summary, every run's minute curves, the line-protocol emission, the batched gzipped push to Influx) is **never** done by chaining MCP calls — it is handed off to the deterministic engine at `${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py` (`export` subcommand), which sweeps the BlazeMeter API directly and pushes via its Influx client module. You resolve scope, verify the Influx environment, run one command, and report its honest stdout accounting.

## Step 0 — Resolve the account, ask the export scope, then census the window

This is the **cross-test** variant of Context Resolution. An export operates over **many tests at once**, so it resolves the **account**, then **asks the user how wide to export** — the **whole account**, a **single workspace**, or a **single project**. It **never assumes** the breadth and never narrows to a single test. **Don't assume:** the user may belong to multiple accounts, names collide across them, and the `blazemeter_user read` default is a suggestion to confirm, never a silent choice.

### Step 0a — Resolve the account (tiered pick rule)

Resolve **only the account** here — which workspaces/projects are exported depends on the scope the user picks in Step 0b. Apply the uniform tiered pick rule:

- Start from the `blazemeter_user read` default account, but **don't assume it's unambiguous — enumerate to see how many accounts exist**: exactly one → **display** it and proceed; more than one → present the pick and **stop** for the user's choice (never silently take the default).
- To enumerate, list one page (`blazemeter_account list`, `limit: 50`).
  - **Fits a choice list** (the first page is *not* full) → present an **interactive choice list**, every entry showing name + id (default marked); if there are more accounts than the choice widget holds, fall back to a **numbered text list** with ids.
  - **Too big / paginated** (the first page comes back full → more pages exist) → **don't dump it**; ask the user to **name, paste an id, or filter**. A pasted **id short-circuits** via a direct `read`; a **name** you resolve by paging and matching.
- Always show the **id** next to each name. **Name doesn't resolve cleanly:** no match → say so, show what *is* available, stop; multiple matches → list each with its id and let the user pick; 403 → report the access gap, don't retry. **Never fall back to the default.**

### Step 0b — Ask the user the export scope (account / workspace / project)

Once the account is confirmed, **ask the user how wide to export — never assume**. Offer three altitudes as a choice list:

- **Whole account** — export every ended run in the account (`--account-id`).
- **A single workspace** — export all its projects' runs (`--workspace-id`). Resolve the workspace with the same tiered pick rule as Step 0a (choice list; **name / paste-id / filter** when the workspace list is large/paginated — e.g. >50).
- **A single project** — export one project's runs (`--project-id`). Resolve workspace → project with the same tiered pick rule.

The scope also determines the **watermark identity** for later syncs — an incremental sync computes "newest already-exported run" per scope tag, so encourage the user to keep syncing at the **same scope level** they backfilled at. **Resolve only the levels the chosen scope needs.**

### Step 0c — AI Consent gate

Check the resolved **account's** AI-consent state via `blazemeter_account read`. If the account has **not** consented, **stop with a clear message** — e.g. `Account Acme (12345) has not enabled AI consent` — before running the census or invoking the engine. (The gate lives here, in the MCP step, on purpose — it must pass **before** any bulk pull runs.)

### Step 0d — Census the window with `blazemeter_execution search` (the practicality checkpoint)

Do **not** enumerate the test catalog — activity is what costs, so the census is **window-first** and stays in the MCP: one server-side-filtered `blazemeter_execution search` reports how many runs fall in the window.

- Call `blazemeter_execution search` with `account_id` (always), plus `workspace_id_list: [<id>]` for a workspace scope or `project_id_list: [<id>]` for a project scope. Express the window as `time_frame` (`latest` = today, `last24`, `lastWeek`, `lastMonth`, or `custom` with `start_time`/`end_time`).
- Read the response's **`total` as the runs-in-window census**. The rows are discovery metadata only (names, times, projects — no test ids, verdicts, or KPIs); the engine computes everything downstream.
- Window filtering is **day-granular**: presets snap the start to midnight, and a `custom` window snaps both bounds to midnight with the **end day exclusive** — pass `end_time` as the day *after* the window end, or the final day's runs are dropped. An approximate census is fine; the engine applies the exact timestamps.

**Practicality guard:** show the census to the user. Export cost scales with the census `total`, and the intra-run curves multiply it — each exported run adds a timeseries fetch and one line per run-minute. When the census runs past **~500 runs in the window**, pause and offer two levers before proceeding: **`--no-timeseries`** (summary-only export — one `bzm_run` line per run, no curves) or a **narrower window/scope** (backfill in slices). **Never silently thin the data** — the user chooses; a full export of a big window is legitimate if they want it.

(If the user asked for a non-default window, resolve it — Step 2 — *before* running the census, so the census counts the right window.)

### Step 0e — Display the resolved scope and the census, then continue

Display the cross-test context block before acting, so the run is auditable:

```
Scope:      Workspace <name>  (ID: <workspace_id>)     ← or "Whole account" / "Project <name> (ID)"
Account:    <account name>  (ID: <account_id>)
Window:     <from> → <to>                              ← or "sync since Influx watermark − lookback"
Activity:   ~<N> runs in the window                    ← from the search census
Mode:       backfill | sync   ·   curves: on | off (--no-timeseries)
Influx:     <INFLUX_URL host> · org <INFLUX_ORG> · bucket <INFLUX_BUCKET>
```

Carry this resolved scope forward as **conversational memory** for later skills in the same conversation (display it, allow a one-step "switch"); **never persist it** to disk.

## Step 1 — Verify the InfluxDB destination (environment-only)

The Influx side is configured **only via environment variables** — never on the command line, never stored by this skill:

| Variable | Meaning |
| --- | --- |
| `INFLUX_URL` | Base URL of the InfluxDB 2.x instance, e.g. `http://localhost:8086` |
| `INFLUX_TOKEN` | API token with write (and read, for sync's watermark query) access |
| `INFLUX_ORG` | Organization name or id |
| `INFLUX_BUCKET` | Default destination bucket |

Check them without touching the network:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_influx.py check
```

It prints `set`/`MISSING` per variable — **names only, never values** — and exits `2` if anything is missing. If variables are missing, show the user which ones and stop; never ask them to paste a token into the conversation.

**No InfluxDB yet?** A local Docker instance takes one command — have the user pick their own password and token (placeholders below, never suggest real-looking values):

```bash
docker run -d --name influxdb -p 8086:8086 \
  -e DOCKER_INFLUXDB_INIT_MODE=setup \
  -e DOCKER_INFLUXDB_INIT_USERNAME=admin \
  -e DOCKER_INFLUXDB_INIT_PASSWORD=<choose-a-password> \
  -e DOCKER_INFLUXDB_INIT_ORG=<your-org> \
  -e DOCKER_INFLUXDB_INIT_BUCKET=bzm \
  -e DOCKER_INFLUXDB_INIT_ADMIN_TOKEN=<choose-a-token> \
  influxdb:2
```

Then:

```bash
export INFLUX_URL=http://localhost:8086
export INFLUX_ORG=<your-org>
export INFLUX_BUCKET=bzm
export INFLUX_TOKEN=<choose-a-token>   # the same value as DOCKER_INFLUXDB_INIT_ADMIN_TOKEN
```

The Data Explorer UI is at `http://localhost:8086` (log in with the username/password above).

**Buckets — v1 keeps it simple:** everything lands in the single `INFLUX_BUCKET` (or `--bucket` override). For **production retention**, the recommended split is two buckets — `bzm_runs` with **infinite retention** for the compact one-point-per-run summaries, and `bzm_points` with **~90-day retention** for the high-volume minute curves — wired as `--bucket bzm_runs --points-bucket bzm_points`. `--points-bucket` routes only the `bzm_run_point` lines; sync's watermark always reads from the main bucket, so the retention split never breaks incremental sync.

## Step 2 — Choose the mode and window

**Backfill (default)** — an explicit window, for first loads and gap-fills. Default window: **last 7 days ending now** (the engine's own default when `--from`/`--to` are omitted). Go deeper (e.g. "backfill the whole quarter") **only when the user explicitly asks** — re-run the Step 0d census for the bigger window first, and for very large backfills suggest slicing into week-sized `--from`/`--to` chunks so a failure loses at most one slice (re-running a slice is harmless — writes are idempotent).

**Sync (`--sync`)** — incremental, for keeping the bucket current. No window flags: the engine queries Influx for the **newest `bzm_run` timestamp already in the bucket for this scope** (the watermark) and exports from **watermark − lookback** to now. `--lookback` is in **seconds** (default `86400` = 24h); the overlap deliberately re-scans recent history so a run that was still in flight last time — or a late-arriving one — is picked up once it ends. Overlap is free: Influx upserts points with an identical series key + timestamp, so re-exported runs simply overwrite themselves. **Recovery = re-run** — after any failure (network, partial push, crashed cron), just run the same sync again; idempotence fills the gap. A fresh bucket with no watermark falls back to the default 7-day window, never a full-history walk.

Constraints the engine enforces (don't fight them): `--sync` is mutually exclusive with `--from`/`--to`, and `--sync` cannot be combined with `--dry-run` (the watermark needs Influx access; preview a dry run with an explicit window instead).

## Step 3 — Run the export

One engine invocation does the whole pull, emission, and push:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_fetch.py export \
  --workspace-id <id>                        # or --account-id / --project-id (exactly one; match Step 0b)
# backfill: add --from 2026-06-25T00:00:00Z --to 2026-07-02T00:00:00Z   (omit both = last 7 days)
# sync:     add --sync            (optionally --lookback <seconds>, default 86400)
# options:  --no-timeseries | --bucket <name> | --points-bucket <name> | --max-failure-rate <0..1>
# preview:  --dry-run --out <scratch>/export.lp   (writes line protocol to the file, pushes nothing)
```

- The engine reads **BlazeMeter credentials from the environment** — `API_KEY_ID` + `API_KEY_SECRET`, or `BLAZEMETER_API_KEY` (a path to a JSON key file) — the same variables the MCP uses, and the **Influx destination from the `INFLUX_*` variables** (Step 1). Never pass any credential on the command line. On a credentials error, show which variables to set and stop.
- **`--dry-run --out <file>.lp`** writes the raw line protocol to a scratch file instead of pushing — useful to show the user exactly what would land in their database before the first real push. Without `--dry-run`, `--out` is ignored (the engine says so on stderr).
- Stdout is a short honest accounting — show it to the user verbatim: ended runs found (and still-running skipped), `bzm_run` + `bzm_run_point` line counts with curve coverage, skipped runs by reason (`archived | no KPIs | fetch failed`), fetch coverage, and (on a push) `pushed N/M lines (X failed batches, Y retries)`.
- **Exit codes:** `0` success; `2` usage/configuration (bad flags, missing credentials or `INFLUX_*` — tell the user what to fix); `3` incomplete — fetch-failure rate above `--max-failure-rate` (default `0.2`), a failed watermark query, or failed Influx write batches. On `3`, report the export as **partial, never complete**, and remind the user that **re-running the same command fills the gap** (idempotent writes). The non-zero exit is deliberate so unattended cron runs can alarm.

## Step 4 — Verify and hand over the queries

After a successful push, point the user at their Influx Data Explorer (`INFLUX_URL` in a browser) and hand them starter Flux queries. **The schema in one breath:** tags are **immutable ids only** (`account_id`, `workspace_id`, `project_id`, `test_id`, `execution_id`, `status`); every display name (`test_name`, `project_name`, `workspace_name`, `execution_name`) is a **field**, so renames never split a series — dashboards resolve the current display name via `last()` on the name field.

`bzm_run` fields: `avg_ms`, `p90_ms`, `p95_ms`, `p99_ms`, `throughput_rps`, `error_rate_pct`, `max_users`, `hits`, `duration_s`, plus the four name fields — timestamped at the run's **end**. `bzm_run_point` fields: `users`, `hits_per_s`, `error_rate_pct`, `avg_ms`, `p95_ms` — one point per run-minute.

**Per-test trend** (p95 across every run of one test):

```flux
from(bucket: "bzm")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "bzm_run" and r.test_id == "<test_id>")
  |> filter(fn: (r) => r._field == "p95_ms")
```

**Display-name mapping** (current name per `test_id` — join or use as a variable/legend source in dashboards):

```flux
from(bucket: "bzm")
  |> range(start: -90d)
  |> filter(fn: (r) => r._measurement == "bzm_run" and r._field == "test_name")
  |> group(columns: ["test_id"])
  |> last()
  |> keep(columns: ["test_id", "_value"])
```

**Project rollup** (daily mean p95 per test across a project):

```flux
from(bucket: "bzm")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "bzm_run" and r.project_id == "<project_id>")
  |> filter(fn: (r) => r._field == "p95_ms")
  |> group(columns: ["test_id"])
  |> aggregateWindow(every: 1d, fn: mean, createEmpty: false)
```

**Workspace rollup** (daily run counts per project and status — `status` is a tag, so pass/fail splits are free):

```flux
from(bucket: "bzm")
  |> range(start: -30d)
  |> filter(fn: (r) => r._measurement == "bzm_run" and r.workspace_id == "<workspace_id>")
  |> filter(fn: (r) => r._field == "duration_s")
  |> group(columns: ["project_id", "status"])
  |> aggregateWindow(every: 1d, fn: count, createEmpty: false)
```

To zoom into one run's minute-by-minute shape, switch the measurement to `bzm_run_point` and filter on `execution_id`.

## Step 5 — Offer the cron line for continuous sync (user-owned scheduling)

This skill does **not** install automation — scheduling belongs to the user. When they want the bucket kept current unattended, hand them the exact one-liner (hourly example). Two substitutions before you present it: expand `${CLAUDE_PLUGIN_ROOT}` to its **actual absolute path** (cron does not define it), and use the scope id resolved in Step 0. Cron jobs don't inherit shell profiles, so the credentials load from a user-owned env file (mode `600`, containing the `API_KEY_ID`/`API_KEY_SECRET` and `INFLUX_*` exports — the user creates it themselves; never write or echo its contents):

```
0 * * * * . "$HOME/.bzm-influx.env" && python3 <plugin-root>/shared/scripts/bzm_fetch.py export --workspace-id <workspace_id> --sync >> "$HOME/bzm-influx-sync.log" 2>&1
```

Any exit `2`/`3` leaves an error in the log (and cron's mail, where configured); because re-runs are idempotent, the next hourly tick usually self-heals a transient failure — a persistent one is worth reading the log for.

## Output template

```
## BlazeMeter → InfluxDB export — <scope name> (ID: <id>)
**Mode:** backfill <from> → <to>  |  or: sync (watermark − <lookback>s)
**Account:** <account name> (<account_id>)   |   **Curves:** on | off
**Destination:** <INFLUX_URL host> · org <org> · bucket <bucket>[ · points → <points-bucket>]

### Result
- Ended runs exported: <N> of <M> in window (<K> still running — next sync catches them)
- Lines pushed: <N> bzm_run + <P> bzm_run_point (curves for <C>/<S> runs)
- Skipped: <a> archived · <b> no KPIs · <f> fetch failed
- Coverage: <ok>/<attempted> fetches ok            ← flag loudly if exit code was 3: "PARTIAL — re-run to fill"

### Next steps
- Explore: <INFLUX_URL> → Data Explorer → bucket <bucket>   (starter Flux queries above)
- Keep it current: the cron line in this conversation (hourly sync, idempotent)
```

For a dry run, replace the destination/result lines with the `.lp` file path and line counts, and say plainly that **nothing was pushed**.

## Gotchas

- **Never do the bulk pull over MCP.** Chaining `blazemeter_*` calls per run would take thousands of payloads at real sizes. MCP is for Step 0's picks, the consent gate, and the census — the engine does everything else in one invocation.
- **Ids are tags, names are fields — on purpose.** A tag change splits an Influx series; test/project renames are common, so only immutable ids are tags. Dashboards get display names from the name *fields* via `last()` (cookbook above). Don't advise adding name tags.
- **Tag cardinality is bounded but real.** `execution_id` as a tag means one series per run — the intended grain for this data, fine at load-testing volumes (thousands of runs, not millions). Don't add higher-cardinality tags (labels, URLs) to the schema.
- **The census is guidance, not the export count.** `blazemeter_execution search` totals can **undercount non-performance runs** that the export's own listing still finds, and its window is **day-granular** (custom windows snap to midnight, **end day exclusive** — pass the day *after* the end). Expect the engine's "ended runs in window" to differ modestly; the ~500-run guard only needs the right order of magnitude.
- **Still-running runs are excluded, not lost.** A run without an end time has no final KPIs and no timestamp to stamp; it is counted as skipped, and the next sync's lookback overlap exports it once it ends. This is also why sync mode exists — don't hand-pick windows to chase in-flight runs.
- **Archived reports are a data condition, not a failure.** BlazeMeter archives old execution reports; those runs are counted distinctly (`archived`) and never inflate the fetch-failure rate or masquerade as "no KPIs".
- **Gap buckets emit no points.** A minute bucket with no samples yields **no** `bzm_run_point` line (and a null KPI is an omitted field, never 0) — so a collection gap can never chart as a zero-throughput outage. Gaps in a dashboard line are honest.
- **Credentials are environment-only — both sides.** BlazeMeter: `API_KEY_ID`/`API_KEY_SECRET` or `BLAZEMETER_API_KEY` (key-file path). Influx: `INFLUX_URL`/`INFLUX_TOKEN`/`INFLUX_ORG`/`INFLUX_BUCKET`. Never on the command line, never in the conversation, never in the cron file you display — the env-file pattern exists so the crontab itself carries no secrets.
- **Dry run and push are different contracts.** `--dry-run --out` writes line protocol locally and touches Influx not at all — which is why `--sync --dry-run` is rejected (the watermark lives in Influx). Without `--dry-run`, `--out` is ignored.
- **Exit 3 means partial, and re-running is the fix.** Too many fetch failures, a failed watermark read, or failed write batches all exit `3` with the truth on stderr. Writes are idempotent upserts — re-run the identical command; never present a `3` as success.
- **Never persist scope.** The resolved account/workspace/project is conversational memory only. Scratch files (dry-run `.lp`) go in the session scratch directory; the only durable artifacts this skill produces live in the user's InfluxDB.
