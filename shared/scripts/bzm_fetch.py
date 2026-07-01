#!/usr/bin/env python3
"""Deterministic bulk-fetch engine for BlazeMeter data-plane sweeps.

Skills use the BlazeMeter MCP for the *control plane* — resolving scope, the AI-consent
gate, single-object drill-ins. Any **data-driven fan-out** ("for each test, list its
executions; for each execution, fetch its reports") runs here instead: one invocation
does the whole sweep against the BlazeMeter REST API v4 and emits ONE compact,
pre-aggregated JSON (size O(tests), not O(executions x sub-reports)), so the model
never ingests raw bulk payloads. See `docs/adr/0019-*.md` and `API_NOTES.md` (endpoint
contract) next to this file.

Subcommands:

  plan   Fast scope census — workspace/project/test COUNTS only (uses each list
         endpoint's `total` field; no executions, no reports). Feeds the skill's
         "this is 800 tests, narrow or proceed?" checkpoint. Prints JSON to stdout.

  sweep  The full pipeline: enumerate tests in scope -> list each test's executions
         and keep those overlapping [--from, --to) -> fetch summary/errors/
         request-stats/anomalies per kept run -> resolve each test's baseline
         (pins > committed file > last passing) -> compute KPI deltas and verdicts
         -> write the digest JSON to --out. Stdout is a five-line human summary.

  history  One test's run history: list the test's executions, keep those
         overlapping [--from, --to) (same paging/stop rule and status buckets as
         sweep) -> fetch each KPI-bucket run's summary -> resolve the baseline
         (same precedence as sweep) -> per-run KPIs + deltas vs the baseline,
         anomaly status per run, incident candidates -> write the history JSON
         (size O(runs-kept), oldest-first) to --out. Stdout is a five-line summary.

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
    python bzm_fetch.py plan  --account-id 12345
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

SCHEMA_VERSION = 1
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

    def _list_all(self, path: str, params: dict, stage: str) -> list[dict]:
        """Page a v4 list endpoint to completion (scope-level: failures raise)."""
        items: list[dict] = []
        skip = 0
        while True:
            page = self.t.get(
                path, {**params, "limit": PAGE_SIZE, "skip": skip, "sort[]": "-updated"}
            )
            self.cov.ok()
            batch = page.get("result") or []
            items.extend(batch)
            skip += PAGE_SIZE
            total = page.get("total")
            if not batch or (total is not None and skip >= total):
                return items

    def count(self, path: str, params: dict) -> int:
        """One-request census via the envelope's `total` field."""
        page = self.t.get(path, {**params, "limit": 1, "skip": 0})
        self.cov.ok()
        total = page.get("total")
        return total if total is not None else len(page.get("result") or [])

    # - scope enumeration -

    def enumerate_tests(self, scope: dict) -> list[dict]:
        """Resolve the scope to [{test, project, workspace}] rows.

        Root listings raise (scope-level failure); per-branch listing failures are
        recorded in coverage and that branch is skipped, like any other fetch.
        """
        rows: list[dict] = []

        def tests_of(project: dict, workspace: dict | None):
            try:
                tests = self._list_all("/tests", {"projectId": project["id"]}, "tests")
            except Exception as exc:  # noqa: BLE001 - recorded, branch skipped
                self.cov.fail("tests", "project:%s" % project.get("id"), exc)
                return
            for test in tests:
                rows.append({"test": test, "project": project, "workspace": workspace})

        if scope.get("project_id"):
            project = self.t.get("/projects/%s" % scope["project_id"]).get("result") or {}
            self.cov.ok()
            tests_of(project, None)
            return rows

        if scope.get("workspace_id"):
            workspaces = [{"id": scope["workspace_id"], "name": None}]
        else:
            workspaces = self._list_all(
                "/workspaces", {"accountId": scope["account_id"]}, "workspaces"
            )

        for ws in workspaces:
            try:
                projects = self._list_all("/projects", {"workspaceId": ws["id"]}, "projects")
            except Exception as exc:  # noqa: BLE001
                self.cov.fail("projects", "workspace:%s" % ws.get("id"), exc)
                continue
            list(self.pool.map(lambda p, w=ws: tests_of(p, w), projects))
        return rows

    # - executions & reports -

    def masters_for_test(self, test_id, from_ts: int, max_pages: int = 20) -> list[dict]:
        """List a test's masters newest-first, stopping once a page predates the window.

        `max_pages` also serves the baseline lookback: history beyond the window is
        fetched anyway until the stop condition, capped so one hyperactive test
        cannot stall the sweep.
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
                for a in (payload["result"].get("anomalies") or [])[:10]:
                    entry["incident_candidates"].append(
                        {
                            "type": "anomaly",
                            "execution_id": entry["candidate_execution_id"],
                            "kpi": a.get("kpi"),
                            "label": a.get("labelName"),
                        }
                    )

    return entry


# --- subcommands -------------------------------------------------------------------


def _scope_from_args(args) -> dict:
    return {
        "account_id": getattr(args, "account_id", None),
        "workspace_id": getattr(args, "workspace_id", None),
        "project_id": getattr(args, "project_id", None),
    }


def cmd_plan(args, transport) -> int:
    cov = Coverage()
    sweeper = Sweeper(transport, cov, concurrency=args.concurrency)
    scope = _scope_from_args(args)

    if scope["project_id"]:
        tests = sweeper.count("/tests", {"projectId": scope["project_id"]})
        plan = {"scope": scope, "projects": 1, "tests": tests}
    else:
        if scope["workspace_id"]:
            workspaces = [{"id": scope["workspace_id"], "name": None}]
        else:
            workspaces = sweeper._list_all(
                "/workspaces", {"accountId": scope["account_id"]}, "workspaces"
            )
        per_ws = []
        for ws in workspaces:
            projects = sweeper._list_all("/projects", {"workspaceId": ws["id"]}, "projects")
            tests = sum(
                sweeper.pool.map(
                    lambda p: sweeper.count("/tests", {"projectId": p["id"]}), projects
                )
            )
            per_ws.append(
                {
                    "workspace_id": ws["id"],
                    "workspace_name": ws.get("name"),
                    "projects": len(projects),
                    "tests": tests,
                }
            )
        plan = {
            "scope": scope,
            "workspaces": len(workspaces),
            "projects": sum(w["projects"] for w in per_ws),
            "tests": sum(w["tests"] for w in per_ws),
            "per_workspace": per_ws,
        }
    print(json.dumps(plan, indent=2))
    return 0


def _load_pins_and_baseline(args) -> tuple[dict[str, str], dict[str, str]]:
    pins: dict[str, str] = {}
    if args.pins:
        raw = json.loads(Path(args.pins).read_text(encoding="utf-8"))
        pins = {str(k): str(v) for k, v in raw.items()}
    baseline_file = bzm_baseline.load_baseline(args.baseline_file) if args.baseline_file else {}
    return pins, baseline_file


def cmd_sweep(args, transport) -> int:
    from_ts, to_ts = parse_when(args.from_), parse_when(args.to)
    if from_ts >= to_ts:
        print("error: --from must be earlier than --to", file=sys.stderr)
        return 2

    pins, baseline_file = _load_pins_and_baseline(args)

    cov = Coverage()
    sweeper = Sweeper(transport, cov, concurrency=args.concurrency)
    rows = sweeper.enumerate_tests(_scope_from_args(args))

    def process(row):
        masters = sweeper.masters_for_test(row["test"]["id"], from_ts)
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

    entries = [e for e in sweeper.pool.map(process, rows) if e is not None]
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
    digest = {
        "schema_version": SCHEMA_VERSION,
        "scope": _scope_from_args(args),
        "window": {"from": from_ts, "to": to_ts},
        "tests_in_scope": len(rows),
        "tests_ran": len(entries),
        "idle_tests": len(rows) - len(entries),
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
        "sweep: %d tests in scope, %d ran in window, %d runs rolled up"
        % (len(rows), len(entries), sum(e["kpi_runs"] for e in entries))
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
                        for a in (payload["result"].get("anomalies") or [])[:10]
                    ]
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
        "(scope census + windowed cross-test digest). Credentials come from "
        "API_KEY_ID/API_KEY_SECRET or BLAZEMETER_API_KEY (key-file path).",
    )
    sub = parser.add_subparsers(dest="command")

    p_plan = sub.add_parser("plan", help="Fast scope census: workspace/project/test counts only.")
    _add_scope_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_sweep = sub.add_parser(
        "sweep", help="Full windowed sweep -> pre-aggregated digest JSON at --out."
    )
    _add_scope_args(p_sweep)
    _add_window_args(p_sweep, out_help="path for the digest JSON")
    p_sweep.set_defaults(func=cmd_sweep)

    p_history = sub.add_parser(
        "history",
        help="One test's windowed run history -> per-run KPI/delta series JSON at --out.",
    )
    p_history.add_argument("--test-id", required=True, help="the test whose runs to pull")
    p_history.add_argument(
        "--concurrency", type=int, default=8, help="parallel fetches (default 8)"
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
