#!/usr/bin/env python3
"""Deterministic bulk-fetch engine for BlazeMeter data-plane sweeps.

Skills use the BlazeMeter MCP for the *control plane* — resolving scope, the AI-consent
gate, single-object drill-ins. Any **data-driven fan-out** ("for each test, list its
executions; for each execution, fetch its reports") runs here instead: one invocation
does the whole sweep against the BlazeMeter REST API v4 and emits ONE compact,
pre-aggregated JSON (size O(tests), not O(executions x sub-reports)), so the model
never ingests raw bulk payloads. See `docs/adr/0019-*.md` and `API_NOTES.md` (endpoint
contract) next to this file.

Subcommands (the window census that used to live here as `plan` moved to the
BlazeMeter MCP's account-wide `blazemeter_execution search` — v1.3.0):

  sweep  The full pipeline, window-first: one filtered /masters listing finds
         every in-window run across the scope (idle tests are never touched) ->
         group by testId -> fetch summary/request-stats/anomalies per kept run
         -> resolve each ACTIVE test's baseline (pins > committed file > last
         passing, via that test's own history) -> compute KPI deltas, verdicts,
         and per-test health chips -> roll entries up into workspace/project
         nodes -> write the digest JSON to --out. Stdout is a five-line
         human summary.

  run-pair  Compare exactly two executions (baseline vs candidate): fetch both
         masters, summary KPIs, request stats, and anomaly status -> compute
         per-KPI and per-endpoint deltas plus verdict inputs -> write ONE compact
         compare JSON to --out. Stdout is a five-line human summary.

  history  One test's run history: list the test's executions, keep those
         overlapping [--from, --to) (same paging/stop rule and status buckets as
         sweep) -> fetch each KPI-bucket run's summary -> resolve the baseline
         (same precedence as sweep) -> per-run KPIs + deltas vs the baseline,
         anomaly status per run, incident candidates -> write the history JSON
         (size O(runs-kept), oldest-first) to --out. Stdout is a five-line summary.

  Intra-run timeseries (--timeseries, on history and run-pair): additionally pull
         each selected run's within-run KPI curves — the minute-by-minute data
         behind the platform's live execution charts — via the ALL label's
         /kpi-values series (interval=60), and attach a compact `timeseries`
         block per run: a downsampled curve (column arrays, capped at
         MAX_CURVE_POINTS buckets) plus a deterministic shape summary (ramp,
         steady-state phases, p95 slope, spikes, saturation). history caps the
         pull at the newest --curve-runs KPI runs (candidate and in-window
         baseline always included) so cost and JSON size stay O(curve-runs),
         never O(all runs x datapoints).

Credentials (same environment variables the BlazeMeter MCP uses; never on argv,
never echoed):

  API_KEY_ID + API_KEY_SECRET   preferred
  BLAZEMETER_API_KEY            fallback: PATH to a JSON key file {"id":..., "secret":...}

Exit codes: 0 success; 2 usage/credential errors; 3 scope-level fetch failure or
fetch-failure rate above --max-failure-rate (a partially-written --out is still
valid JSON with its `coverage` block telling the truth).

Standard-library only (urllib + ThreadPoolExecutor), like every script in this
directory, so users need nothing installed. All HTTP goes through one seam
(`Transport.get`) so tests inject canned fixtures and never touch the network.

Usage:
    python bzm_fetch.py --help
    python bzm_fetch.py sweep --project-id 777 \
        --from 2026-06-30T09:00:00Z --to 2026-07-01T09:00:00Z \
        --baseline-file .blazemeter/baseline.json --out digest.json
    python bzm_fetch.py history --test-id 15725552 \
        --from 2026-06-01T00:00:00Z --to 2026-07-01T00:00:00Z --out history.json
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import bzm_baseline

SCHEMA_VERSION = 4  # v4: optional intra-run `timeseries` blocks on history/run-pair (--timeseries)
BASE_URL = "https://a.blazemeter.com/api/v4"
PAGE_SIZE = 50  # documented max for every v4 list endpoint (see API_NOTES.md)
USER_AGENT = "perforce-skills-bzm-fetch/1"

# reportStatus buckets (see API_NOTES.md). Only complete verdicts roll into KPIs;
# abort/error runs have partial data that would distort the scorecard.
KPI_STATUSES = frozenset({"pass", "fail"})
PARTIAL_STATUSES = frozenset({"abort", "error"})
INCONCLUSIVE_STATUSES = frozenset({"unset", "noData"})

REGRESSION_THRESHOLD_PCT = 10.0  # a >=10% adverse KPI move counts as a regression
ERROR_SPIKE_PCT = 1.0  # overall error-rate bar for an incident candidate
ERROR_SPIKE_SEVERE_PCT = 5.0
ENDPOINT_SPIKE_RATE = 95.0  # a label erroring on >=95% of >=MIN samples
ENDPOINT_SPIKE_MIN_SAMPLES = 20

# Health chips (codified from the skills' prose thresholds). Order = severity,
# worst first — a rollup's health is the worst of its members'.
SLA_CRITICAL_PCT = 60.0
SLA_AT_RISK_PCT = 90.0
HEALTH_ORDER = ("critical", "at-risk", "unjudged", "healthy")

# Intra-run timeseries (--timeseries). interval=60 keeps one point per minute —
# the coarsest bucket /kpi-values documents, and the docs' own recommendation for
# managing dataset size. Curves are further downsampled to MAX_CURVE_POINTS
# buckets so a soak run cannot blow up the JSON, and history pulls curves for at
# most DEFAULT_CURVE_RUNS runs (see --curve-runs).
TIMESERIES_INTERVAL_S = 60
MAX_CURVE_POINTS = 60
DEFAULT_CURVE_RUNS = 5
RAMP_PEAK_FRACTION = 0.95  # "at peak" = within 5% of the run's max active users
RT_SPIKE_FACTOR = 2.0  # a bucket's avg RT >= 2x the run median counts as a spike
ERROR_BURST_MIN_PCT = 5.0  # an error burst must cross 5% in its bucket...
ERROR_BURST_FACTOR = 2.0  # ...and be at least 2x the run's overall error rate

# Incident-candidate severity for the rollups' `worst_incident` ref: outright
# failures, then regressions vs baseline, then error spikes, then anomalies —
# the same ranking the skills apply in prose.
INCIDENT_SEVERITY = {"failure": 0, "regression": 1, "error_spike": 2,
                     "endpoint_error_spike": 3, "anomaly": 4}


class FetchError(Exception):
    """A scope-level failure the sweep cannot proceed past (auth, root listing)."""


class CredentialsError(FetchError):
    """Missing or unreadable credentials."""


# --- credentials --------------------------------------------------------------


def load_credentials(env: dict[str, str] | None = None) -> tuple[str, str]:
    """Resolve (key_id, key_secret) from the MCP's own env vars.

    `API_KEY_ID` + `API_KEY_SECRET` win; else `BLAZEMETER_API_KEY` names a JSON key
    file `{"id": ..., "secret": ...}`. The secret never appears in errors or output.
    """
    env = os.environ if env is None else env
    key_id, key_secret = env.get("API_KEY_ID"), env.get("API_KEY_SECRET")
    if key_id and key_secret:
        return key_id, key_secret
    key_path = env.get("BLAZEMETER_API_KEY")
    if key_path:
        try:
            data = json.loads(Path(key_path).read_text(encoding="utf-8"))
            return str(data["id"]), str(data["secret"])
        except (OSError, ValueError, KeyError) as exc:
            raise CredentialsError(
                "could not read a {'id', 'secret'} JSON key file from the path in "
                "BLAZEMETER_API_KEY: %s" % type(exc).__name__
            ) from exc
    raise CredentialsError(
        "no credentials: set API_KEY_ID + API_KEY_SECRET, or BLAZEMETER_API_KEY "
        "(a path to a JSON key file), the same variables the BlazeMeter MCP uses"
    )


# --- transport (the single HTTP seam; tests replace this object) ---------------


class Transport:
    """All BlazeMeter HTTP goes through `get`. Retries 429/5xx with backoff."""

    def __init__(
        self,
        key_id: str,
        key_secret: str,
        base_url: str = BASE_URL,
        timeout: float = 30.0,
        max_attempts: int = 3,
        sleep=time.sleep,
    ):
        token = base64.b64encode(("%s:%s" % (key_id, key_secret)).encode()).decode()
        self._headers = {"Authorization": "Basic " + token, "User-Agent": USER_AGENT}
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._max_attempts = max_attempts
        self._sleep = sleep

    def get(self, path: str, params: dict | None = None) -> dict:
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()))
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                req = urllib.request.Request(url, headers=self._headers)
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_exc = exc
                if exc.code not in (429,) and exc.code < 500:
                    raise  # 4xx other than 429 will not improve with retries
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                last_exc = exc
                retry_after = None
            if attempt < self._max_attempts:
                try:
                    delay = float(retry_after) if retry_after else 2.0 ** (attempt - 1)
                except ValueError:
                    delay = 2.0 ** (attempt - 1)
                self._sleep(delay)
        raise last_exc  # type: ignore[misc]


# --- pure helpers (fixture-tested without any transport) -----------------------


def parse_when(value: str) -> int:
    """Parse an ISO-8601 timestamp (Z or offset) or epoch seconds into epoch seconds."""
    value = value.strip()
    if value.isdigit():
        return int(value)
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def run_bucket(master: dict) -> str:
    """Classify a master: kpi | partial | inconclusive | running."""
    if not master.get("ended"):
        return "running"
    status = str(master.get("reportStatus") or "unset")
    if status in KPI_STATUSES:
        return "kpi"
    if status in PARTIAL_STATUSES:
        return "partial"
    return "inconclusive"


def overlaps_window(master: dict, from_ts: int, to_ts: int) -> bool:
    start = master.get("created") or 0
    end = master.get("ended") or master.get("updated") or start
    return start < to_ts and end >= from_ts


def page_is_older_than(masters: list[dict], from_ts: int) -> bool:
    """True when every run on the page ended before the window starts (stop paging)."""
    return bool(masters) and all(
        (m.get("ended") or m.get("updated") or m.get("created") or 0) < from_ts
        for m in masters
    )


def summary_all_row(report: dict) -> dict | None:
    """Pick the ALL (aggregate) row out of a /reports/default/summary response."""
    for row in (report.get("result") or {}).get("summary", []) or []:
        if row.get("id") == "ALL" or row.get("lb") == "ALL":
            return row
    return None


def summary_kpis(row: dict) -> dict:
    """Normalize the summary ALL row into the KPI set the digest compares."""
    hits = row.get("hits") or 0
    failed = row.get("failed") or 0
    max_users = row.get("maxUsers") or row.get("concurrency") or 0
    hits_avg = row.get("hits_avg") or 0.0
    return {
        "avg_ms": row.get("avg"),
        "p90_ms": row.get("tp90"),
        "p95_ms": row.get("tp95"),
        "p99_ms": row.get("tp99"),
        "hits": hits,
        "throughput_rps": hits_avg,
        "error_rate_pct": (100.0 * failed / hits) if hits else 0.0,
        "max_users": max_users,
        "rps_per_vu": (hits_avg / max_users) if max_users else None,
        "duration_s": row.get("duration"),
    }


def label_kpis(row: dict, max_users: float | None = None) -> dict:
    """Normalize an aggregate-report label row into the KPI shape `compute_deltas` takes.

    `max_users` is the run-level achieved peak (from the summary ALL row) so a label's
    throughput can be judged per-virtual-user when the two runs' load configs differ,
    the same rule the run-level comparison applies. Rate is derived from
    `errorsCount / samples` (the endpoint's `errorsRate` unit is undocumented).
    """
    samples = row.get("samples") or 0
    errors = row.get("errorsCount") or 0
    throughput = row.get("avgThroughput")
    return {
        "avg_ms": row.get("avgResponseTime"),
        "p90_ms": row.get("90line"),
        "p95_ms": row.get("95line"),
        "p99_ms": row.get("99line"),
        "samples": samples,
        "throughput_rps": throughput,
        "error_rate_pct": (100.0 * errors / samples) if samples else 0.0,
        "max_users": max_users,
        "rps_per_vu": (throughput / max_users) if (throughput is not None and max_users) else None,
    }


def label_rows(agg_payload: dict | None) -> dict[str, dict]:
    """Index an aggregate-report payload by label name (skipping any ALL row)."""
    rows: dict[str, dict] = {}
    for row in (agg_payload or {}).get("result") or []:
        name = row.get("labelName")
        if name and name != "ALL":
            rows[name] = row
    return rows


def pct_change(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline == 0:
        return None
    return 100.0 * (candidate - baseline) / baseline


def compute_deltas(candidate: dict, baseline: dict) -> dict:
    """KPI deltas candidate-vs-baseline; adverse if >=10% in the worse direction.

    Throughput is inverted (lower is worse) and, when the load config differs
    (max_users changed), judged on RPS-per-virtual-user instead of raw RPS so a
    smaller load is not misread as a regression. Error rate is judged in
    percentage points when the baseline was clean (0%), since a relative change
    from zero is undefined.
    """
    deltas: dict[str, dict] = {}
    for key, label in (("avg_ms", "avg"), ("p95_ms", "p95"), ("p99_ms", "p99")):
        change = pct_change(baseline.get(key), candidate.get(key))
        if change is not None:
            deltas[label] = {"pct": round(change, 1), "adverse": change >= REGRESSION_THRESHOLD_PCT}

    normalized = bool(
        candidate.get("max_users")
        and baseline.get("max_users")
        and candidate["max_users"] != baseline["max_users"]
    )
    tp_key = "rps_per_vu" if normalized else "throughput_rps"
    change = pct_change(baseline.get(tp_key), candidate.get(tp_key))
    if change is not None:
        deltas["throughput"] = {
            "pct": round(change, 1),
            "adverse": change <= -REGRESSION_THRESHOLD_PCT,
            "normalized_per_vu": normalized,
        }

    b_err, c_err = baseline.get("error_rate_pct"), candidate.get("error_rate_pct")
    if b_err is not None and c_err is not None:
        if b_err > 0:
            change = 100.0 * (c_err - b_err) / b_err
            deltas["error_rate"] = {
                "pct": round(change, 1),
                "adverse": change >= REGRESSION_THRESHOLD_PCT,
            }
        elif c_err > 0:
            deltas["error_rate"] = {
                "pct": None,
                "points": round(c_err, 2),
                "adverse": c_err >= ERROR_SPIKE_PCT,
                "note": "baseline error rate was 0%%; candidate at %.2f%%" % c_err,
            }
    return deltas


def worst_kpi_move(deltas: dict) -> dict | None:
    """The single largest adverse move, named — the scoreboard column."""
    worst = None
    for name, d in deltas.items():
        if not d.get("adverse"):
            continue
        magnitude = abs(d["pct"]) if d.get("pct") is not None else d.get("points", 0.0)
        if worst is None or magnitude > worst[0]:
            worst = (magnitude, name, d)
    if worst is None:
        return None
    _, name, d = worst
    return {"kpi": name, **{k: v for k, v in d.items() if k != "adverse"}}


def pick_candidate(kpi_runs: list[dict]) -> dict | None:
    """The run judged against the baseline: newest failing run, else newest run."""
    if not kpi_runs:
        return None
    newest = max(kpi_runs, key=lambda m: (m.get("ended") or 0, str(m.get("id"))))
    failing = [m for m in kpi_runs if str(m.get("reportStatus")) == "fail"]
    if failing:
        return max(failing, key=lambda m: (m.get("ended") or 0, str(m.get("id"))))
    return newest


def resolve_test_baseline(
    test_id: str,
    masters: list[dict],
    candidate: dict | None,
    pins: dict[str, str],
    baseline_file: dict[str, str],
) -> dict:
    """Baseline precedence: conversational pin > committed file > last passing run.

    The candidate is excluded from last-passing selection — otherwise a green
    run would always be its own baseline and a still-green regression could
    never be detected. (A *pinned* baseline may still point at the candidate;
    callers surface that as `baseline_is_only_run`.)
    """
    pinned = pins.get(test_id) or baseline_file.get(test_id)
    if pinned is not None:
        return {"source": "pin" if test_id in pins else "file", "execution_id": str(pinned)}
    chosen = bzm_baseline.select_last_passing(
        [
            {"id": m.get("id"), "status": m.get("reportStatus"), "end_time": m.get("ended")}
            for m in masters
            if candidate is None or m.get("id") != candidate.get("id")
        ]
    )
    if chosen:
        return {"source": "last-passing", "execution_id": str(chosen["id"])}
    return {"source": "none", "execution_id": None}


def anomaly_status(payload: dict | None) -> str:
    """Map /anomalies/stats to no_anomalies | anomalies_with_details | statistics_unavailable."""
    if not payload or not isinstance(payload.get("result"), dict):
        return "statistics_unavailable"
    count = payload["result"].get("anomalyCount")
    if count is None:
        return "statistics_unavailable"
    return "anomalies_with_details" if count > 0 else "no_anomalies"


def anomaly_items(payload: dict | None) -> list[dict]:
    """The anomaly rows out of /anomalies/stats, whichever shape the API used.

    Seen live: `result.anomalies` is a LIST when empty but an anomalyId-keyed
    DICT when anomalies exist — normalize both to a list of rows.
    """
    if not payload or not isinstance(payload.get("result"), dict):
        return []
    anomalies = payload["result"].get("anomalies")
    if isinstance(anomalies, dict):
        return list(anomalies.values())
    return list(anomalies or [])


# --- intra-run timeseries (pure helpers; endpoint contract in API_NOTES.md) -------


def all_label_id(payload: dict | None) -> str | None:
    """The aggregate ALL label's id out of a /data/labels response, or None.

    Rows are matched on either naming convention (`id`/`name` or
    `labelId`/`labelName`) since the endpoint is undocumented; anything without
    an exact-"ALL" name is ignored rather than guessed at.
    """
    for row in (payload or {}).get("result") or []:
        if not isinstance(row, dict):
            continue
        name = row.get("name") if "name" in row else row.get("labelName")
        label_id = row.get("id") if "id" in row else row.get("labelId")
        if name == "ALL" and label_id is not None:
            return str(label_id)
    return None


def series_points(payload: dict | None) -> list[dict]:
    """The datapoint rows out of a /kpi-values response, sorted by `ts`.

    Each `result[]` entry describes one requested series; its datapoints live in
    a list-valued field whose name is not pinned down by the docs — find the
    first list of dicts carrying a `ts` and use that. Points without a `ts` are
    dropped (they cannot be placed on the time axis).
    """
    for entry in (payload or {}).get("result") or []:
        if not isinstance(entry, dict):
            continue
        for value in entry.values():
            if (
                isinstance(value, list)
                and value
                and all(isinstance(p, dict) for p in value)
                and any("ts" in p for p in value)
            ):
                points = [p for p in value if p.get("ts") is not None]
                return sorted(points, key=lambda p: p["ts"])
    return []


def _phase_stats(points: list[dict], interval_s: int) -> dict | None:
    """Aggregate KPIs over one phase's minute buckets (None for an empty phase)."""
    if not points:
        return None
    hits = sum(p.get("n") or 0 for p in points)
    errors = sum(p.get("ec") or 0 for p in points)
    avg_pairs = [(p["t_avg"], p.get("n") or 0) for p in points if p.get("t_avg") is not None]
    p95_values = [p["t_pec95"] for p in points if p.get("t_pec95") is not None]
    weight = sum(w for _, w in avg_pairs)
    if weight:
        avg_ms = sum(v * w for v, w in avg_pairs) / weight
    elif avg_pairs:
        avg_ms = sum(v for v, _ in avg_pairs) / len(avg_pairs)
    else:
        avg_ms = None
    return {
        "buckets": len(points),
        "hits_per_s": round(hits / (len(points) * interval_s), 2),
        "error_rate_pct": round(100.0 * errors / hits, 2) if hits else None,
        "avg_ms": round(avg_ms, 1) if avg_ms is not None else None,
        "p95_ms": round(max(p95_values), 1) if p95_values else None,
    }


def downsample_curve(points: list[dict], interval_s: int, max_points: int = MAX_CURVE_POINTS) -> dict:
    """Merge minute buckets into <= max_points columns (offsets from the first bucket).

    Merge rules keep each column honest for its unit: hits/errors are counts
    (summed, then divided by the merged span for a rate), active users is a
    gauge (peak of the span), avg RT is hits-weighted, and the merged p95 is the
    span's WORST bucket p95 — averaging percentiles across buckets would
    understate spikes. Buckets with no data stay null, never a fabricated 0.
    """
    stride = max(1, math.ceil(len(points) / max_points))
    t0 = points[0]["ts"]
    curve: dict[str, list] = {
        "offset_s": [], "users": [], "hits_per_s": [], "error_rate_pct": [],
        "avg_ms": [], "p95_ms": [],
    }
    for i in range(0, len(points), stride):
        group = points[i : i + stride]
        stats = _phase_stats(group, interval_s)
        users = [p["na"] for p in group if p.get("na") is not None]
        curve["offset_s"].append(group[0]["ts"] - t0)
        curve["users"].append(max(users) if users else None)
        curve["hits_per_s"].append(stats["hits_per_s"])
        curve["error_rate_pct"].append(stats["error_rate_pct"])
        curve["avg_ms"].append(stats["avg_ms"])
        curve["p95_ms"].append(stats["p95_ms"])
    return curve


def _slope_per_minute(values: list[tuple[float, float]]) -> float | None:
    """Least-squares slope (per minute) over (minute, value) pairs; None under 3 points."""
    if len(values) < 3:
        return None
    n = len(values)
    mean_x = sum(x for x, _ in values) / n
    mean_y = sum(y for _, y in values) / n
    var_x = sum((x - mean_x) ** 2 for x, _ in values)
    if var_x == 0:
        return None
    cov = sum((x - mean_x) * (y - mean_y) for x, y in values)
    return round(cov / var_x, 2)


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def shape_summary(points: list[dict], interval_s: int) -> dict:
    """Deterministic within-run shape features from the full minute-bucket series.

    Computed on the raw (un-downsampled) buckets so downsampling can never hide
    a spike. Phases: ramp = buckets before active users first reach 95% of the
    run's peak; steady = the rest, split in half to expose degradation over the
    hold. `p95_slope_ms_per_min` is the least-squares p95 slope over the steady
    phase. Spikes/bursts flag buckets far outside the run's own norm (thresholds
    in the module constants); saturation flags throughput peaking a bucket or
    more before users do — the knee the AI should look at.
    """
    offsets = [p["ts"] - points[0]["ts"] for p in points]
    users = [p.get("na") for p in points]
    peak_users = max((u for u in users if u is not None), default=None)

    ramp_end = 0
    if peak_users:
        ramp_end = next(
            i for i, u in enumerate(users) if (u or 0) >= RAMP_PEAK_FRACTION * peak_users
        )
    steady = points[ramp_end:]
    half = len(steady) // 2

    p95_series = [
        (offsets[ramp_end + i] / 60.0, p["t_pec95"])
        for i, p in enumerate(steady)
        if p.get("t_pec95") is not None
    ]

    rt_values = [p["t_avg"] for p in points if p.get("t_avg") is not None]
    rt_median = _median(rt_values)
    rt_spikes = []
    if rt_median:
        rt_spikes = sorted(
            (
                {
                    "offset_s": offsets[i],
                    "avg_ms": round(p["t_avg"], 1),
                    "p95_ms": round(p["t_pec95"], 1) if p.get("t_pec95") is not None else None,
                }
                for i, p in enumerate(points)
                if p.get("t_avg") is not None and p["t_avg"] >= RT_SPIKE_FACTOR * rt_median
            ),
            key=lambda s: -s["avg_ms"],
        )[:3]

    total_hits = sum(p.get("n") or 0 for p in points)
    total_errors = sum(p.get("ec") or 0 for p in points)
    overall_error_pct = (100.0 * total_errors / total_hits) if total_hits else 0.0
    error_bursts = []
    burst_floor = max(ERROR_BURST_MIN_PCT, ERROR_BURST_FACTOR * overall_error_pct)
    for i, p in enumerate(points):
        hits = p.get("n") or 0
        if not hits:
            continue
        rate = 100.0 * (p.get("ec") or 0) / hits
        if rate >= burst_floor:
            error_bursts.append({"offset_s": offsets[i], "error_rate_pct": round(rate, 2)})
    error_bursts = sorted(error_bursts, key=lambda b: -b["error_rate_pct"])[:3]

    rates = [(p.get("n") or 0) / interval_s for p in points]
    peak_rate_idx = max(range(len(rates)), key=lambda i: rates[i]) if any(rates) else None
    time_to_peak_users_s = offsets[ramp_end] if peak_users else None
    saturation = None
    if peak_rate_idx is not None:
        saturation = {
            "throughput_peak_offset_s": offsets[peak_rate_idx],
            "throughput_peaked_before_users": bool(
                time_to_peak_users_s is not None
                and offsets[peak_rate_idx] + interval_s < time_to_peak_users_s
            ),
        }

    return {
        "peak_users": peak_users,
        "time_to_peak_users_s": time_to_peak_users_s,
        "phases": {
            "ramp": _phase_stats(points[:ramp_end], interval_s),
            "steady_early": _phase_stats(steady[: half or len(steady)], interval_s),
            "steady_late": _phase_stats(steady[half:], interval_s) if half else None,
        },
        "p95_slope_ms_per_min": _slope_per_minute(p95_series),
        "rt_spikes": rt_spikes,
        "error_bursts": error_bursts,
        "saturation": saturation,
    }


def build_timeseries(points: list[dict], interval_s: int = TIMESERIES_INTERVAL_S) -> dict | None:
    """One run's compact `timeseries` block: downsampled curve + shape summary."""
    if not points:
        return None
    return {
        "interval_s": interval_s,
        "start_ts": points[0]["ts"],
        "buckets": len(points),
        "curve": downsample_curve(points, interval_s),
        "shape": shape_summary(points, interval_s),
    }


def health_for(kpi_runs: int, passed: int, failed: int, regressed: bool) -> str:
    """Deterministic health chip for one test's window (schema v3).

    `unjudged` when no run produced a KPI verdict (only inconclusive/partial/
    running runs) — never "healthy", there is nothing to judge. `critical` on
    any failing run or SLA compliance (passed/kpi_runs) below 60%; `at-risk`
    when regressed-while-green or SLA below 90%; else `healthy`.
    """
    if kpi_runs == 0:
        return "unjudged"
    sla_pct = 100.0 * passed / kpi_runs
    if failed > 0 or sla_pct < SLA_CRITICAL_PCT:
        return "critical"
    if regressed or sla_pct < SLA_AT_RISK_PCT:
        return "at-risk"
    return "healthy"


def worst_health(healths) -> str:
    """The worst member health, in HEALTH_ORDER (critical > at-risk > unjudged > healthy)."""
    healths = set(healths)
    for h in HEALTH_ORDER:
        if h in healths:
            return h
    return "unjudged"  # an empty group has nothing judged


# --- coverage accounting --------------------------------------------------------


@dataclass
class Coverage:
    """Thread-safe fetch bookkeeping; the digest's honesty block."""

    attempted: int = 0
    failed: int = 0
    failures: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def ok(self):
        with self._lock:
            self.attempted += 1

    def fail(self, stage: str, item: str, exc: Exception):
        with self._lock:
            self.attempted += 1
            self.failed += 1
            self.failures.append({"stage": stage, "item": item, "error": type(exc).__name__})

    def rate(self) -> float:
        return (self.failed / self.attempted) if self.attempted else 0.0


# --- the sweeper -----------------------------------------------------------------


class Sweeper:
    def __init__(self, transport, coverage: Coverage, concurrency: int = 8):
        self.t = transport
        self.cov = coverage
        self.pool = ThreadPoolExecutor(max_workers=max(1, concurrency))

    # - paged lists -

    # - windowed scope listing (server-side time filter; the sweep's entry point) -

    def masters_in_window(
        self, scope: dict, from_ts: int, to_ts: int, max_pages: int = 200
    ) -> list[dict]:
        """List every master in the scope that overlaps [from, to).

        One paged `/masters` listing with the server-side `startTime`/`endTime`
        filter (epoch seconds) — cost scales with runs-in-window, never with the
        size of the test catalog. Master rows carry `testId`, `projectId`,
        `reportStatus`, and `maxUsers`, so no per-test iteration is needed.
        Results are re-filtered client-side (overlap semantics, matching
        `judge_test`), which also guards against the filter being ignored: the
        listing is newest-first, so a page entirely older than the window stops
        paging, and `max_pages` (10,000 runs) bounds a pathological walk. The
        account-wide listing may omit `total`, so a short page is the normal stop.

        Failure posture: the FIRST page raising is a scope-level failure and
        propagates (bad credentials/scope must not read as an empty window); a
        failure on a LATER page degrades — the pages already fetched are
        returned and the failure lands in coverage, so a partial digest beats
        no digest.
        """
        scope_param = (
            {"projectId": scope["project_id"]}
            if scope.get("project_id")
            else {"workspaceId": scope["workspace_id"]}
            if scope.get("workspace_id")
            else {"accountId": scope["account_id"]}
        )
        masters: list[dict] = []
        skip = 0
        for _ in range(max_pages):
            try:
                page = self.t.get(
                    "/masters",
                    {
                        **scope_param,
                        "limit": PAGE_SIZE,
                        "skip": skip,
                        "sort[]": "-created",
                        "startTime": from_ts,
                        "endTime": to_ts,
                    },
                )
                self.cov.ok()
            except Exception as exc:  # noqa: BLE001 - later pages degrade into coverage
                if skip == 0:
                    raise
                self.cov.fail("masters", "window-page:%d" % skip, exc)
                break
            batch = page.get("result") or []
            masters.extend(m for m in batch if overlaps_window(m, from_ts, to_ts))
            if len(batch) < PAGE_SIZE or page_is_older_than(batch, from_ts):
                break
            skip += PAGE_SIZE
        return masters

    def project_context(self, project_id, cache: dict, workspace_cache: dict) -> dict:
        """Resolve {project, workspace} names for grouping, cached per project id."""
        if project_id in cache:
            return cache[project_id]
        context = {"project": {"id": project_id, "name": None}, "workspace": None}
        try:
            project = self.t.get("/projects/%s" % project_id).get("result") or {}
            self.cov.ok()
            context["project"] = {"id": project.get("id", project_id), "name": project.get("name")}
            if project.get("workspaceId"):
                ws_id = project["workspaceId"]
                context["workspace"] = {
                    "id": ws_id,
                    "name": self.workspace_name(ws_id, workspace_cache),
                }
        except Exception as exc:  # noqa: BLE001 - names are cosmetic, ids still group
            self.cov.fail("project", "project:%s" % project_id, exc)
        cache[project_id] = context
        return context

    def workspace_name(self, workspace_id, cache: dict) -> str | None:
        """Workspace name from /workspaces/{id}, cached per workspace id.

        One request per DISTINCT active workspace (cost scales with activity,
        never the account's workspace catalog). A failed read degrades to a
        null name via coverage — the name is cosmetic, the id still groups.
        """
        if workspace_id in cache:
            return cache[workspace_id]
        try:
            workspace = self.t.get("/workspaces/%s" % workspace_id).get("result") or {}
            self.cov.ok()
            name = workspace.get("name")
        except Exception as exc:  # noqa: BLE001 - name is cosmetic, id still groups
            self.cov.fail("workspace", "workspace:%s" % workspace_id, exc)
            name = None
        cache[workspace_id] = name
        return name

    def test_name(self, test_id, cache: dict, fallback: str | None) -> str | None:
        """Canonical test name from /tests/{id}, cached; falls back to the run label.

        Master rows carry a per-run label that can be a custom execution name or
        go stale after a rename — the /tests object is the identity the
        scoreboard should show. One request per ACTIVE test (cost scales with
        activity, like everything else here); a failed read degrades to the
        newest run's label via coverage.
        """
        if test_id in cache:
            return cache[test_id]
        try:
            test = self.t.get("/tests/%s" % test_id).get("result") or {}
            self.cov.ok()
            name = test.get("name") or fallback
        except Exception as exc:  # noqa: BLE001 - name is cosmetic, id still groups
            self.cov.fail("test", "test:%s" % test_id, exc)
            name = fallback
        cache[test_id] = name
        return name

    # - executions & reports -

    def masters_for_test(
        self, test_id, from_ts: int, max_pages: int = 20, stop_when=None
    ) -> list[dict]:
        """List a test's masters newest-first, stopping once a page predates the window.

        `max_pages` also serves the baseline lookback: history beyond the window is
        fetched anyway until the stop condition, capped so one hyperactive test
        cannot stall the sweep. `stop_when(masters)` lets a caller end paging as
        soon as the accumulated rows satisfy it (e.g. a passing run for baseline
        resolution has appeared).
        """
        masters: list[dict] = []
        skip = 0
        for _ in range(max_pages):
            try:
                page = self.t.get(
                    "/masters",
                    {"testId": test_id, "limit": PAGE_SIZE, "skip": skip, "sort[]": "-updated"},
                )
                self.cov.ok()
            except Exception as exc:  # noqa: BLE001
                self.cov.fail("masters", "test:%s" % test_id, exc)
                break
            batch = page.get("result") or []
            masters.extend(batch)
            if not batch or page_is_older_than(batch, from_ts):
                break
            if stop_when is not None and stop_when(masters):
                break
            skip += PAGE_SIZE
            total = page.get("total")
            if total is not None and skip >= total:
                break
        return masters

    def fetch_summary(self, master_id) -> dict | None:
        try:
            report = self.t.get("/masters/%s/reports/default/summary" % master_id)
            self.cov.ok()
        except Exception as exc:  # noqa: BLE001
            self.cov.fail("summary", "master:%s" % master_id, exc)
            return None
        row = summary_all_row(report)
        if not row:
            return None
        kpis = summary_kpis(row)
        # Some run types (e.g. GUI/EUX) return an ALL row full of nulls; zero hits
        # means there are no load KPIs to compare — report "unavailable", never a
        # fabricated 0% error rate.
        return kpis if kpis["hits"] else None

    def fetch_timeseries(self, master_id) -> dict | None:
        """One run's intra-run timeseries block, or None (degrades, never fails).

        Two GETs per run: /data/labels to find the aggregate ALL label, then ONE
        /kpi-values series for it at interval=60 — each datapoint carries the
        full KPI field set (users, hits, errors, avg, percentiles), so one
        request covers every curve. A missing ALL label, empty series, or HTTP
        failure returns None; callers surface it as `timeseries_unavailable`.
        """
        try:
            labels = self.t.get("/data/labels", {"master_id": master_id})
            self.cov.ok()
        except Exception as exc:  # noqa: BLE001 - curves degrade, never fail the pull
            self.cov.fail("timeseries_labels", "master:%s" % master_id, exc)
            return None
        label_id = all_label_id(labels)
        if label_id is None:
            return None
        try:
            series = self.t.get(
                "/masters/%s/kpi-values" % master_id,
                {"id": "label/%s/t/pec95" % label_id, "interval": TIMESERIES_INTERVAL_S},
            )
            self.cov.ok()
        except Exception as exc:  # noqa: BLE001 - curves degrade, never fail the pull
            self.cov.fail("timeseries", "master:%s" % master_id, exc)
            return None
        return build_timeseries(series_points(series))

    def fetch_optional(self, path: str, stage: str, master_id) -> dict | None:
        """Errors/aggregate/anomalies: absence degrades the digest, never fails it."""
        try:
            payload = self.t.get(path % master_id)
            self.cov.ok()
            return payload
        except Exception as exc:  # noqa: BLE001
            self.cov.fail(stage, "master:%s" % master_id, exc)
            return None


# --- per-test judgment (pure given fetched inputs) --------------------------------


def judge_test(
    row: dict,
    masters: list[dict],
    from_ts: int,
    to_ts: int,
    pins: dict[str, str],
    baseline_file: dict[str, str],
    fetch_summary,
    fetch_optional,
    include_anomalies: bool = True,
) -> dict | None:
    """Roll one test up into its digest entry; None when it was idle in the window."""
    test = row["test"]
    test_id = str(test.get("id"))

    in_window = [m for m in masters if overlaps_window(m, from_ts, to_ts)]
    buckets: dict[str, list[dict]] = {"kpi": [], "partial": [], "inconclusive": [], "running": []}
    for m in in_window:
        buckets[run_bucket(m)].append(m)
    if not in_window:
        return None

    kpi_runs = buckets["kpi"]
    passed = sum(1 for m in kpi_runs if str(m.get("reportStatus")) == "pass")

    candidate = pick_candidate(kpi_runs)
    baseline = resolve_test_baseline(test_id, masters, candidate, pins, baseline_file)

    entry: dict = {
        "test_id": test_id,
        "test_name": test.get("name"),
        "project": {"id": row["project"].get("id"), "name": row["project"].get("name")},
        "workspace": (
            {"id": row["workspace"].get("id"), "name": row["workspace"].get("name")}
            if row.get("workspace")
            else None
        ),
        "runs_in_window": len(in_window),
        "kpi_runs": len(kpi_runs),
        "passed": passed,
        "failed": len(kpi_runs) - passed,
        "skipped_partial": len(buckets["partial"]),
        "inconclusive": len(buckets["inconclusive"]),
        "still_running": len(buckets["running"]),
        "baseline": baseline,
        "candidate_execution_id": str(candidate["id"]) if candidate else None,
        "regressed": False,
        "deltas": {},
        "worst_kpi_move": None,
        "notes": [],
        "incident_candidates": [],
    }

    candidate_kpis = fetch_summary(candidate["id"]) if candidate else None
    if candidate_kpis:
        entry["candidate_kpis"] = candidate_kpis
    elif candidate:
        entry["notes"].append("candidate_kpis_unavailable")

    if candidate and baseline["execution_id"]:
        if str(candidate["id"]) == baseline["execution_id"]:
            entry["notes"].append("baseline_is_only_run")
        else:
            baseline_kpis = fetch_summary(baseline["execution_id"])
            if baseline_kpis and candidate_kpis:
                entry["deltas"] = compute_deltas(candidate_kpis, baseline_kpis)
                entry["baseline_kpis"] = baseline_kpis
                entry["worst_kpi_move"] = worst_kpi_move(entry["deltas"])
                entry["regressed"] = entry["worst_kpi_move"] is not None
            elif candidate_kpis:
                entry["notes"].append("baseline_kpis_unavailable")
    elif baseline["execution_id"] is None:
        entry["notes"].append("no_baseline")

    # Incident candidates — the deterministic inputs to the AI's severity ranking.
    for m in kpi_runs:
        if str(m.get("reportStatus")) == "fail":
            entry["incident_candidates"].append(
                {"type": "failure", "execution_id": str(m.get("id"))}
            )
    if entry["regressed"]:
        entry["incident_candidates"].append(
            {
                "type": "regression",
                "execution_id": entry["candidate_execution_id"],
                "worst_kpi_move": entry["worst_kpi_move"],
            }
        )
    if candidate_kpis and candidate_kpis["error_rate_pct"] >= ERROR_SPIKE_PCT:
        entry["incident_candidates"].append(
            {
                "type": "error_spike",
                "execution_id": entry["candidate_execution_id"],
                "error_rate_pct": round(candidate_kpis["error_rate_pct"], 2),
                "severe": candidate_kpis["error_rate_pct"] >= ERROR_SPIKE_SEVERE_PCT,
            }
        )

    if candidate:
        agg = fetch_optional(
            "/masters/%s/reports/aggregatereport/data", "request_stats", candidate["id"]
        )
        for label in (agg or {}).get("result") or []:
            samples = label.get("samples") or 0
            errors = label.get("errorsCount") or 0
            # Rate is derived from errorsCount/samples rather than trusting the
            # endpoint's `errorsRate` field, whose unit (fraction vs percent) is
            # not documented — counts are unambiguous.
            rate_pct = (100.0 * errors / samples) if samples else 0.0
            if samples >= ENDPOINT_SPIKE_MIN_SAMPLES and rate_pct >= ENDPOINT_SPIKE_RATE:
                entry["incident_candidates"].append(
                    {
                        "type": "endpoint_error_spike",
                        "execution_id": entry["candidate_execution_id"],
                        "label": label.get("labelName"),
                        "errors_rate_pct": round(rate_pct, 1),
                        "samples": samples,
                    }
                )

        if include_anomalies:
            payload = fetch_optional("/masters/%s/anomalies/stats", "anomalies", candidate["id"])
            status = anomaly_status(payload)
            entry["anomaly_status"] = status
            if status == "anomalies_with_details":
                for a in anomaly_items(payload)[:10]:
                    entry["incident_candidates"].append(
                        {
                            "type": "anomaly",
                            "execution_id": entry["candidate_execution_id"],
                            "kpi": a.get("kpi"),
                            "label": a.get("labelName"),
                        }
                    )

    entry["health"] = health_for(len(kpi_runs), passed, entry["failed"], entry["regressed"])
    return entry


# --- subcommands -------------------------------------------------------------------


def _scope_from_args(args) -> dict:
    return {
        "account_id": getattr(args, "account_id", None),
        "workspace_id": getattr(args, "workspace_id", None),
        "project_id": getattr(args, "project_id", None),
    }


def _load_pins_and_baseline(args) -> tuple[dict[str, str], dict[str, str]]:
    pins: dict[str, str] = {}
    if args.pins:
        raw = json.loads(Path(args.pins).read_text(encoding="utf-8"))
        pins = {str(k): str(v) for k, v in raw.items()}
    baseline_file = bzm_baseline.load_baseline(args.baseline_file) if args.baseline_file else {}
    return pins, baseline_file


def _incident_rank(candidate: dict) -> tuple:
    """Sort key for incident candidates: severity class first, bigger magnitude first."""
    kind = str(candidate.get("type"))
    if kind == "regression":
        magnitude = _move_magnitude(candidate.get("worst_kpi_move"))
    elif kind == "error_spike":
        magnitude = candidate.get("error_rate_pct") or 0.0
    elif kind == "endpoint_error_spike":
        magnitude = candidate.get("errors_rate_pct") or 0.0
    else:
        magnitude = 0.0
    return (INCIDENT_SEVERITY.get(kind, len(INCIDENT_SEVERITY)), -magnitude)


def worst_incident_ref(entries: list[dict]) -> dict | None:
    """Short ref {type, test_id, execution_id} to the entries' top-severity incident."""
    best = None
    for entry in entries:
        for candidate in entry.get("incident_candidates") or []:
            rank = _incident_rank(candidate)
            if best is None or rank < best[0]:
                best = (
                    rank,
                    {
                        "type": candidate.get("type"),
                        "test_id": entry["test_id"],
                        "execution_id": candidate.get("execution_id"),
                    },
                )
    return best[1] if best else None


def rollup_nodes(entries: list[dict]) -> tuple[list[dict], list[dict]]:
    """Aggregate sweep entries into the v3 `workspaces[]` / `projects[]` rollups.

    Grouping keys are the ids already resolved on each entry — derived from
    whatever context is known, never invented: an entry whose workspace could
    not be resolved contributes no workspace row (so a project-scoped sweep may
    yield an empty or single-row `workspaces[]`), and its project row carries a
    null workspace. A node's health is the worst of its members' (HEALTH_ORDER);
    `worst_incident` refs the members' top-severity incident candidate.
    """

    def node(info: dict, members: list[dict]) -> dict:
        return {
            "id": info.get("id"),
            "name": info.get("name"),
            "runs_in_window": sum(m["runs_in_window"] for m in members),
            "tests_ran": len(members),
            "passed": sum(m["passed"] for m in members),
            "failed": sum(m["failed"] for m in members),
            "regressed": sum(1 for m in members if m["regressed"]),
            "inconclusive": sum(m["inconclusive"] for m in members),
            "health": worst_health(m["health"] for m in members),
            "worst_incident": worst_incident_ref(members),
        }

    def sort_key(row: dict) -> tuple:
        return (HEALTH_ORDER.index(row["health"]), -row["failed"], -row["regressed"], str(row["id"]))

    by_workspace: dict = {}
    by_project: dict = {}
    for entry in entries:
        project = entry["project"]
        group = by_project.setdefault(
            project.get("id"), {"info": project, "workspace": entry.get("workspace"), "members": []}
        )
        group["members"].append(entry)
        workspace = entry.get("workspace")
        if workspace and workspace.get("id") is not None:
            by_workspace.setdefault(workspace["id"], {"info": workspace, "members": []})[
                "members"
            ].append(entry)

    workspaces = sorted((node(g["info"], g["members"]) for g in by_workspace.values()), key=sort_key)
    projects = sorted(
        ({**node(g["info"], g["members"]), "workspace": g["workspace"]} for g in by_project.values()),
        key=sort_key,
    )
    return workspaces, projects


def cmd_sweep(args, transport) -> int:
    from_ts, to_ts = parse_when(args.from_), parse_when(args.to)
    if from_ts >= to_ts:
        print("error: --from must be earlier than --to", file=sys.stderr)
        return 2

    pins, baseline_file = _load_pins_and_baseline(args)

    cov = Coverage()
    sweeper = Sweeper(transport, cov, concurrency=args.concurrency)
    scope = _scope_from_args(args)

    # One server-side-filtered listing finds every run in the window across the
    # whole scope — idle tests are never touched, so cost scales with activity.
    # Rows without a testId can't be judged or drilled into; drop them up front
    # so every count in the digest derives from the same set.
    window_masters = [
        m for m in sweeper.masters_in_window(scope, from_ts, to_ts) if m.get("testId") is not None
    ]
    by_test: dict = {}
    for m in window_masters:
        by_test.setdefault(str(m.get("testId")), []).append(m)

    context_cache: dict = {}
    workspace_cache: dict = {}
    name_cache: dict = {}

    def process(item):
        test_id, in_window = item
        # Baseline lookback (last-passing) needs history beyond the window — but
        # only when nothing pins this test's baseline, only for ACTIVE tests, and
        # only when the window itself holds no non-candidate passing run (a pass
        # already in the window is always newer than any pre-window pass).
        kpi_in = [m for m in in_window if run_bucket(m) == "kpi"]
        candidate = pick_candidate(kpi_in)
        cand_id = str(candidate.get("id")) if candidate else None

        def has_baseline_pass(masters):
            return any(
                str(m.get("reportStatus")) == "pass" and str(m.get("id")) != cand_id
                for m in masters
            )

        if test_id in pins or test_id in baseline_file or has_baseline_pass(in_window):
            masters = in_window
        else:
            masters = (
                sweeper.masters_for_test(test_id, from_ts, stop_when=has_baseline_pass)
                or in_window
            )
        context = sweeper.project_context(
            in_window[0].get("projectId"), context_cache, workspace_cache
        )
        row = {
            "test": {
                "id": test_id,
                "name": sweeper.test_name(test_id, name_cache, in_window[0].get("name")),
            },
            "project": context["project"],
            "workspace": context["workspace"],
        }
        return judge_test(
            row,
            masters,
            from_ts,
            to_ts,
            pins,
            baseline_file,
            sweeper.fetch_summary,
            sweeper.fetch_optional,
            include_anomalies=not args.no_anomalies,
        )

    entries = [e for e in sweeper.pool.map(process, by_test.items()) if e is not None]
    # Failures first, then biggest adverse move — the scoreboard's sort order.
    entries.sort(
        key=lambda e: (
            -e["failed"],
            -(abs(e["worst_kpi_move"]["pct"]) if e["worst_kpi_move"] and e["worst_kpi_move"].get("pct") else 0),
        )
    )

    anomalies_unavailable = sum(
        1 for e in entries if e.get("anomaly_status") == "statistics_unavailable"
    )
    workspace_rollup, project_rollup = rollup_nodes(entries)
    digest = {
        "schema_version": SCHEMA_VERSION,
        "scope": scope,
        "window": {"from": from_ts, "to": to_ts},
        "runs_in_window": len(window_masters),
        "tests_ran": len(entries),
        "workspaces": workspace_rollup,
        "projects": project_rollup,
        "tests": entries,
        "coverage": {
            "http_attempted": cov.attempted,
            "http_failed": cov.failed,
            "failures": cov.failures[:50],
            "skipped_partial_runs": sum(e["skipped_partial"] for e in entries),
            "inconclusive_runs": sum(e["inconclusive"] for e in entries),
            "anomalies_unavailable": anomalies_unavailable,
        },
    }

    out = Path(args.out)
    out.write_text(json.dumps(digest, indent=2) + "\n", encoding="utf-8")

    failures = sum(e["failed"] for e in entries)
    regressed = sum(1 for e in entries if e["regressed"])
    no_baseline = sum(1 for e in entries if e["baseline"]["source"] == "none")
    print(
        "sweep: %d runs in window across %d tests, %d runs rolled up"
        % (len(window_masters), len(entries), sum(e["kpi_runs"] for e in entries))
    )
    print(
        "failing runs: %d | newly regressed tests: %d | no baseline: %d"
        % (failures, regressed, no_baseline)
    )
    print(
        "coverage: %d/%d fetches ok (%d failed) | partial runs skipped: %d"
        % (cov.attempted - cov.failed, cov.attempted, cov.failed, digest["coverage"]["skipped_partial_runs"])
    )
    print("wrote %s" % out)

    if cov.rate() > args.max_failure_rate:
        print(
            "error: fetch failure rate %.0f%% exceeds --max-failure-rate %.0f%% — "
            "digest written but incomplete" % (100 * cov.rate(), 100 * args.max_failure_rate),
            file=sys.stderr,
        )
        return 3
    return 0


def _move_magnitude(move: dict | None) -> float:
    """Comparable size of a worst-KPI move (pct when relative, points when from zero)."""
    if not move:
        return 0.0
    if move.get("pct") is not None:
        return abs(move["pct"])
    return move.get("points", 0.0) or 0.0


def _format_move(move: dict | None) -> str:
    if not move:
        return "none"
    if move.get("pct") is not None:
        return "%s %+.1f%%" % (move["kpi"], move["pct"])
    return "%s +%.2f points (baseline was 0%%)" % (move["kpi"], move.get("points", 0.0))


def cmd_run_pair(args, transport) -> int:
    baseline_id, candidate_id = str(args.baseline_id), str(args.candidate_id)
    if baseline_id == candidate_id:
        print(
            "error: --baseline-id and --candidate-id are both %s — a run cannot be "
            "compared to itself; pick a different baseline execution" % baseline_id,
            file=sys.stderr,
        )
        return 2

    cov = Coverage()
    sweeper = Sweeper(transport, cov, concurrency=4)
    notes: list[str] = []

    def fetch_side(side: str, master_id: str):
        """One run's master + summary KPIs + request stats + anomaly status.

        The master read is required (without it the run cannot be identified);
        every sub-report degrades into coverage/notes instead of failing the compare.
        """
        try:
            master = sweeper.t.get("/masters/%s" % master_id).get("result") or {}
            cov.ok()
        except Exception as exc:  # noqa: BLE001 - reported, compare aborts cleanly
            cov.fail("master", "master:%s" % master_id, exc)
            return None, {}
        kpis = sweeper.fetch_summary(master_id)
        if kpis is None:
            notes.append("%s_kpis_unavailable" % side)
        agg = sweeper.fetch_optional(
            "/masters/%s/reports/aggregatereport/data", "request_stats", master_id
        )
        if agg is None:
            notes.append("%s_request_stats_unavailable" % side)
        run = {
            "execution_id": master_id,
            "name": master.get("name"),
            "test_id": str(master["testId"]) if master.get("testId") is not None else None,
            "report_status": str(master.get("reportStatus") or "unset"),
            "created": master.get("created"),
            "ended": master.get("ended"),
            "still_running": not master.get("ended"),
            "kpis": kpis,
        }
        if run["still_running"]:
            notes.append("%s_still_running" % side)
        if not args.no_anomalies:
            payload = sweeper.fetch_optional(
                "/masters/%s/anomalies/stats", "anomalies", master_id
            )
            run["anomaly_status"] = anomaly_status(payload)
        if args.timeseries:
            run["timeseries"] = sweeper.fetch_timeseries(master_id)
            if run["timeseries"] is None:
                notes.append("%s_timeseries_unavailable" % side)
        return run, label_rows(agg)

    baseline, b_labels = fetch_side("baseline", baseline_id)
    candidate, c_labels = fetch_side("candidate", candidate_id)
    missing = [i for i, r in ((baseline_id, baseline), (candidate_id, candidate)) if r is None]
    if missing:
        print(
            "error: could not read execution %s — check the id and your access"
            % " and ".join(missing),
            file=sys.stderr,
        )
        return 3

    # Run-level KPI deltas: only when BOTH summaries yielded load KPIs. A zero-hit
    # or null summary row means "no KPIs to compare", never a fabricated clean 0%.
    deltas = (
        compute_deltas(candidate["kpis"], baseline["kpis"])
        if baseline["kpis"] and candidate["kpis"]
        else {}
    )

    # Per-endpoint deltas where the label exists in both runs with samples.
    matched = []
    for name in sorted(set(b_labels) & set(c_labels)):
        b_row, c_row = b_labels[name], c_labels[name]
        if not (b_row.get("samples") and c_row.get("samples")):
            continue
        b_k = label_kpis(b_row, (baseline["kpis"] or {}).get("max_users"))
        c_k = label_kpis(c_row, (candidate["kpis"] or {}).get("max_users"))
        d = compute_deltas(c_k, b_k)
        matched.append(
            {
                "label": name,
                "baseline": b_k,
                "candidate": c_k,
                "deltas": d,
                "worst_kpi_move": worst_kpi_move(d),
            }
        )
    matched.sort(key=lambda e: -_move_magnitude(e["worst_kpi_move"]))
    endpoints = {
        "matched": matched,
        "baseline_only": sorted(set(b_labels) - set(c_labels)),
        "candidate_only": sorted(set(c_labels) - set(b_labels)),
    }

    adverse = sorted(k for k, d in deltas.items() if d.get("adverse"))
    load_differs = bool(
        baseline["kpis"]
        and candidate["kpis"]
        and baseline["kpis"].get("max_users")
        and candidate["kpis"].get("max_users")
        and baseline["kpis"]["max_users"] != candidate["kpis"]["max_users"]
    )
    verdict_inputs = {
        "baseline_report_status": baseline["report_status"],
        "candidate_report_status": candidate["report_status"],
        "candidate_failed_while_baseline_passed": (
            candidate["report_status"] == "fail" and baseline["report_status"] == "pass"
        ),
        "adverse_kpi_moves": adverse,
        "worst_kpi_move": worst_kpi_move(deltas),
        "regressed": bool(adverse),
        "load_config_differs": load_differs,
        "endpoints_with_adverse_moves": [
            e["label"] for e in matched if any(d.get("adverse") for d in e["deltas"].values())
        ],
    }

    compare = {
        "schema_version": SCHEMA_VERSION,
        "baseline": baseline,
        "candidate": candidate,
        "kpi_deltas": deltas,
        "endpoints": endpoints,
        "verdict_inputs": verdict_inputs,
        "notes": notes,
        "coverage": {
            "http_attempted": cov.attempted,
            "http_failed": cov.failed,
            "failures": cov.failures[:50],
        },
    }
    out = Path(args.out)
    out.write_text(json.dumps(compare, indent=2) + "\n", encoding="utf-8")

    print(
        "run-pair: baseline %s (%s) vs candidate %s (%s)"
        % (baseline_id, baseline["report_status"], candidate_id, candidate["report_status"])
    )
    print(
        "kpis: baseline %s | candidate %s | load config %s"
        % (
            "ok" if baseline["kpis"] else "unavailable",
            "ok" if candidate["kpis"] else "unavailable",
            "differs (throughput judged per-VU)" if load_differs else "matches",
        )
    )
    print(
        "adverse moves: %s | worst: %s"
        % (", ".join(adverse) if adverse else "none", _format_move(verdict_inputs["worst_kpi_move"]))
    )
    print(
        "coverage: %d/%d fetches ok (%d failed)"
        % (cov.attempted - cov.failed, cov.attempted, cov.failed)
    )
    print("wrote %s" % out)
    return 0


def cmd_history(args, transport) -> int:
    """One test's windowed run history -> per-run KPI/delta series JSON at --out."""
    from_ts, to_ts = parse_when(args.from_), parse_when(args.to)
    if from_ts >= to_ts:
        print("error: --from must be earlier than --to", file=sys.stderr)
        return 2

    pins, baseline_file = _load_pins_and_baseline(args)
    cov = Coverage()
    sweeper = Sweeper(transport, cov, concurrency=args.concurrency)
    test_id = str(args.test_id)

    # Scope-level read: a bad test id must fail loudly, not read as "idle test".
    test = transport.get("/tests/%s" % test_id).get("result") or {}
    cov.ok()

    masters = sweeper.masters_for_test(test_id, from_ts)
    in_window = sorted(
        (m for m in masters if overlaps_window(m, from_ts, to_ts)),
        key=lambda m: (m.get("ended") or m.get("updated") or m.get("created") or 0, str(m.get("id"))),
    )
    buckets: dict[str, list[dict]] = {"kpi": [], "partial": [], "inconclusive": [], "running": []}
    for m in in_window:
        buckets[run_bucket(m)].append(m)
    kpi_runs = buckets["kpi"]
    passed = sum(1 for m in kpi_runs if str(m.get("reportStatus")) == "pass")

    candidate = pick_candidate(kpi_runs)
    baseline = resolve_test_baseline(test_id, masters, candidate, pins, baseline_file)

    # Per-run summaries for KPI-bucket runs only (partial runs would distort the
    # trend; inconclusive runs have nothing to fetch). One parallel pass.
    kpi_ids = [str(m.get("id")) for m in kpi_runs]
    kpis_by_id = dict(zip(kpi_ids, sweeper.pool.map(sweeper.fetch_summary, kpi_ids)))

    notes: list[str] = []
    baseline_kpis = None
    if baseline["execution_id"] is None:
        notes.append("no_baseline")
    else:
        if candidate and str(candidate["id"]) == baseline["execution_id"]:
            notes.append("baseline_is_only_run")
        if baseline["execution_id"] in kpis_by_id:
            baseline_kpis = kpis_by_id[baseline["execution_id"]]
        else:
            baseline_kpis = sweeper.fetch_summary(baseline["execution_id"])
        if baseline_kpis is None:
            notes.append("baseline_kpis_unavailable")

    anomalies_by_id: dict[str, dict | None] = {}
    if not args.no_anomalies:
        anomalies_by_id = dict(
            zip(
                kpi_ids,
                sweeper.pool.map(
                    lambda mid: sweeper.fetch_optional("/masters/%s/anomalies/stats", "anomalies", mid),
                    kpi_ids,
                ),
            )
        )

    # Intra-run curves for a bounded set only: the newest --curve-runs KPI runs,
    # with the candidate and an in-window baseline always included — cost and
    # JSON size stay O(curve-runs), never O(all runs x datapoints). Runs outside
    # the selection simply carry no `timeseries` key (distinct from a selected
    # run whose pull failed, which is noted `timeseries_unavailable`).
    timeseries_by_id: dict[str, dict | None] = {}
    if args.timeseries and kpi_runs:
        newest_first = sorted(
            kpi_runs,
            key=lambda m: (m.get("ended") or 0, str(m.get("id"))),
            reverse=True,
        )
        curve_ids = [str(m.get("id")) for m in newest_first[: max(1, args.curve_runs)]]
        for forced in (str(candidate["id"]) if candidate else None, baseline["execution_id"]):
            if forced and forced not in curve_ids and forced in kpi_ids:
                curve_ids.append(forced)
        timeseries_by_id = dict(
            zip(curve_ids, sweeper.pool.map(sweeper.fetch_timeseries, curve_ids))
        )

    runs: list[dict] = []
    incident_candidates: list[dict] = []
    regressed_runs = 0
    for m in in_window:
        run_id = str(m.get("id"))
        bucket = run_bucket(m)
        entry: dict = {
            "execution_id": run_id,
            "name": m.get("name"),
            "started": m.get("created"),
            "ended": m.get("ended"),
            "report_status": str(m.get("reportStatus") or "unset"),
            "bucket": bucket,
            "kpis": None,
            "deltas": {},
            "worst_kpi_move": None,
            "regressed": False,
            "is_baseline": run_id == baseline["execution_id"],
            "notes": [],
        }
        if bucket == "kpi":
            kpis = kpis_by_id.get(run_id)
            entry["kpis"] = kpis
            if kpis is None:
                entry["notes"].append("kpis_unavailable")
            elif baseline_kpis and not entry["is_baseline"]:
                entry["deltas"] = compute_deltas(kpis, baseline_kpis)
                entry["worst_kpi_move"] = worst_kpi_move(entry["deltas"])
                entry["regressed"] = entry["worst_kpi_move"] is not None
                if entry["regressed"]:
                    regressed_runs += 1
                    incident_candidates.append(
                        {
                            "type": "regression",
                            "execution_id": run_id,
                            "worst_kpi_move": entry["worst_kpi_move"],
                        }
                    )
            if str(m.get("reportStatus")) == "fail":
                incident_candidates.append({"type": "failure", "execution_id": run_id})
            if kpis and kpis["error_rate_pct"] >= ERROR_SPIKE_PCT:
                incident_candidates.append(
                    {
                        "type": "error_spike",
                        "execution_id": run_id,
                        "error_rate_pct": round(kpis["error_rate_pct"], 2),
                        "severe": kpis["error_rate_pct"] >= ERROR_SPIKE_SEVERE_PCT,
                    }
                )
            if not args.no_anomalies:
                payload = anomalies_by_id.get(run_id)
                status = anomaly_status(payload)
                entry["anomaly_status"] = status
                if status == "anomalies_with_details":
                    entry["anomalies"] = [
                        {"kpi": a.get("kpi"), "label": a.get("labelName")}
                        for a in anomaly_items(payload)[:10]
                    ]
            if run_id in timeseries_by_id:
                entry["timeseries"] = timeseries_by_id[run_id]
                if entry["timeseries"] is None:
                    entry["notes"].append("timeseries_unavailable")
        runs.append(entry)

    # Per-endpoint spike check on the candidate only; whole-history endpoint
    # drill-ins stay interactive (MCP) so the JSON stays O(runs-kept).
    if candidate:
        agg = sweeper.fetch_optional(
            "/masters/%s/reports/aggregatereport/data", "request_stats", candidate["id"]
        )
        for label in (agg or {}).get("result") or []:
            samples = label.get("samples") or 0
            errors = label.get("errorsCount") or 0
            rate_pct = (100.0 * errors / samples) if samples else 0.0
            if samples >= ENDPOINT_SPIKE_MIN_SAMPLES and rate_pct >= ENDPOINT_SPIKE_RATE:
                incident_candidates.append(
                    {
                        "type": "endpoint_error_spike",
                        "execution_id": str(candidate["id"]),
                        "label": label.get("labelName"),
                        "errors_rate_pct": round(rate_pct, 1),
                        "samples": samples,
                    }
                )

    anomalies_unavailable = sum(
        1 for r in runs if r.get("anomaly_status") == "statistics_unavailable"
    )
    history = {
        "schema_version": SCHEMA_VERSION,
        "test_id": test_id,
        "test_name": test.get("name"),
        "window": {"from": from_ts, "to": to_ts},
        "runs_in_window": len(in_window),
        "kpi_runs": len(kpi_runs),
        "passed": passed,
        "failed": len(kpi_runs) - passed,
        "skipped_partial": len(buckets["partial"]),
        "inconclusive": len(buckets["inconclusive"]),
        "still_running": len(buckets["running"]),
        "baseline": baseline,
        "baseline_kpis": baseline_kpis,
        "candidate_execution_id": str(candidate["id"]) if candidate else None,
        "regressed_runs": regressed_runs,
        "runs": runs,
        "incident_candidates": incident_candidates,
        "notes": notes,
        "coverage": {
            "http_attempted": cov.attempted,
            "http_failed": cov.failed,
            "failures": cov.failures[:50],
            "skipped_partial_runs": len(buckets["partial"]),
            "inconclusive_runs": len(buckets["inconclusive"]),
            "anomalies_unavailable": anomalies_unavailable,
        },
    }
    if args.timeseries:
        history["coverage"]["timeseries_unavailable"] = sum(
            1 for v in timeseries_by_id.values() if v is None
        )

    out = Path(args.out)
    out.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")

    print(
        "history: test %s (%s): %d runs in window, %d with KPI verdicts"
        % (test_id, test.get("name") or "unnamed", len(in_window), len(kpi_runs))
    )
    print(
        "pass/fail: %d/%d | regressed runs: %d | baseline: %s %s"
        % (passed, len(kpi_runs) - passed, regressed_runs, baseline["source"], baseline["execution_id"] or "-")
    )
    print(
        "coverage: %d/%d fetches ok (%d failed) | partial runs skipped: %d"
        % (cov.attempted - cov.failed, cov.attempted, cov.failed, len(buckets["partial"]))
    )
    if args.timeseries:
        print(
            "timeseries: curves for %d/%d selected runs (interval %ds)"
            % (
                sum(1 for v in timeseries_by_id.values() if v is not None),
                len(timeseries_by_id),
                TIMESERIES_INTERVAL_S,
            )
        )
    print("wrote %s" % out)

    if cov.rate() > args.max_failure_rate:
        print(
            "error: fetch failure rate %.0f%% exceeds --max-failure-rate %.0f%% — "
            "history written but incomplete" % (100 * cov.rate(), 100 * args.max_failure_rate),
            file=sys.stderr,
        )
        return 3
    return 0


# --- CLI ------------------------------------------------------------------------


def _add_scope_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--account-id", help="sweep the whole account")
    group.add_argument("--workspace-id", help="sweep one workspace")
    group.add_argument("--project-id", help="sweep one project")
    parser.add_argument("--concurrency", type=int, default=8, help="parallel fetches (default 8)")


def _add_window_args(parser: argparse.ArgumentParser, out_help: str) -> None:
    parser.add_argument("--from", dest="from_", required=True, help="window start (ISO-8601 or epoch)")
    parser.add_argument("--to", required=True, help="window end (ISO-8601 or epoch)")
    parser.add_argument("--out", required=True, help=out_help)
    parser.add_argument("--baseline-file", help="committed .blazemeter/baseline.json path")
    parser.add_argument("--pins", help="JSON file of conversational pins {test_id: execution_id}")
    parser.add_argument(
        "--max-failure-rate",
        type=float,
        default=0.2,
        help="exit non-zero when this fraction of fetches failed (default 0.2)",
    )
    parser.add_argument("--no-anomalies", action="store_true", help="skip anomaly-stats fetches")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic bulk-fetch engine for BlazeMeter sweeps "
        "(windowed cross-test digest, run history, run pairs). Credentials come "
        "from API_KEY_ID/API_KEY_SECRET or BLAZEMETER_API_KEY (key-file path).",
    )
    sub = parser.add_subparsers(dest="command")

    p_sweep = sub.add_parser(
        "sweep", help="Full windowed sweep -> pre-aggregated digest JSON at --out."
    )
    _add_scope_args(p_sweep)
    _add_window_args(p_sweep, out_help="path for the digest JSON")
    p_sweep.set_defaults(func=cmd_sweep)

    p_pair = sub.add_parser(
        "run-pair",
        help="Compare two executions (baseline vs candidate) -> compact compare JSON at --out.",
    )
    p_pair.add_argument("--baseline-id", required=True, help="baseline execution (master) id")
    p_pair.add_argument("--candidate-id", required=True, help="candidate execution (master) id")
    p_pair.add_argument("--out", required=True, help="path for the compare JSON")
    p_pair.add_argument("--no-anomalies", action="store_true", help="skip anomaly-stats fetches")
    p_pair.add_argument(
        "--timeseries",
        action="store_true",
        help="also pull each run's intra-run KPI curves (downsampled + shape summary)",
    )
    p_pair.set_defaults(func=cmd_run_pair)

    p_history = sub.add_parser(
        "history",
        help="One test's windowed run history -> per-run KPI/delta series JSON at --out.",
    )
    p_history.add_argument("--test-id", required=True, help="the test whose runs to pull")
    p_history.add_argument(
        "--concurrency", type=int, default=8, help="parallel fetches (default 8)"
    )
    p_history.add_argument(
        "--timeseries",
        action="store_true",
        help="also pull intra-run KPI curves (downsampled + shape summary) for the "
        "newest --curve-runs KPI runs, candidate and in-window baseline included",
    )
    p_history.add_argument(
        "--curve-runs",
        type=int,
        default=DEFAULT_CURVE_RUNS,
        help="how many runs get intra-run curves with --timeseries (default %d)"
        % DEFAULT_CURVE_RUNS,
    )
    _add_window_args(p_history, out_help="path for the history JSON")
    p_history.set_defaults(func=cmd_history)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1

    try:
        key_id, key_secret = load_credentials()
    except CredentialsError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    transport = Transport(key_id, key_secret)
    try:
        return args.func(args, transport)
    except FetchError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 3
    except urllib.error.HTTPError as exc:
        print("error: scope-level request failed: HTTP %s" % exc.code, file=sys.stderr)
        return 3
    except urllib.error.URLError as exc:
        print("error: could not reach BlazeMeter: %s" % exc.reason, file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
