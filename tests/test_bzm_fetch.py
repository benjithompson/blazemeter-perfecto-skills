"""Tests for the bulk-fetch engine (`shared/scripts/bzm_fetch.py`).

All HTTP goes through the `Transport.get` seam; these tests inject a fixture-backed
fake transport, so nothing here touches the network. The `live`-marked tests at the
bottom are the drift tripwire: they auto-run when BlazeMeter credentials are present
in the environment and auto-skip otherwise (so CI, which has no credentials, stays
green).
"""

from __future__ import annotations

import argparse
import io
import json
import os
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

import bzm_fetch

FIXTURES = Path(__file__).parent / "fixtures" / "bzm_fetch"


class FakeTransport:
    """Serves canned responses from a fixture's `routes` list.

    A route matches when its `path` equals the requested path and every key in its
    `params` (if any) equals the request's param, compared as strings. Unknown
    requests raise, so a fixture gap fails loudly.
    """

    def __init__(self, routes: list[dict]):
        self.routes = routes
        self.calls: list[tuple[str, dict]] = []

    @classmethod
    def from_fixture(cls, name: str) -> "FakeTransport":
        data = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return cls(data["routes"])

    def get(self, path: str, params: dict | None = None) -> dict:
        params = {str(k): str(v) for k, v in (params or {}).items()}
        self.calls.append((path, params))
        for route in self.routes:
            if route["path"] != path:
                continue
            want = {str(k): str(v) for k, v in route.get("params", {}).items()}
            if all(params.get(k) == v for k, v in want.items()):
                return route["response"]
        raise KeyError("no fixture route for GET %s %s" % (path, params))


def sweep_args(tmp_path, **overrides) -> argparse.Namespace:
    defaults = dict(
        account_id=None,
        workspace_id=None,
        project_id="101",
        from_="1000000",
        to="2000000",
        out=str(tmp_path / "digest.json"),
        baseline_file=None,
        pins=None,
        concurrency=2,
        max_failure_rate=0.2,
        no_anomalies=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


# --- pure helpers ---------------------------------------------------------------


def test_parse_when_accepts_iso_z_offset_and_epoch():
    assert bzm_fetch.parse_when("1970-01-01T00:00:00Z") == 0
    assert bzm_fetch.parse_when("1970-01-01T01:00:00+01:00") == 0
    assert bzm_fetch.parse_when("123456") == 123456


def test_run_bucket_classification():
    assert bzm_fetch.run_bucket({"ended": 10, "reportStatus": "pass"}) == "kpi"
    assert bzm_fetch.run_bucket({"ended": 10, "reportStatus": "fail"}) == "kpi"
    assert bzm_fetch.run_bucket({"ended": 10, "reportStatus": "abort"}) == "partial"
    assert bzm_fetch.run_bucket({"ended": 10, "reportStatus": "error"}) == "partial"
    assert bzm_fetch.run_bucket({"ended": 10, "reportStatus": "noData"}) == "inconclusive"
    assert bzm_fetch.run_bucket({"ended": 10}) == "inconclusive"  # unset default
    assert bzm_fetch.run_bucket({"ended": None, "reportStatus": "pass"}) == "running"


def test_window_overlap_and_stop_paging():
    inside = {"created": 1500, "ended": 1600}
    before = {"created": 100, "ended": 200}
    straddles = {"created": 900, "ended": 1100}
    assert bzm_fetch.overlaps_window(inside, 1000, 2000)
    assert not bzm_fetch.overlaps_window(before, 1000, 2000)
    assert bzm_fetch.overlaps_window(straddles, 1000, 2000)
    assert bzm_fetch.page_is_older_than([before], 1000)
    assert not bzm_fetch.page_is_older_than([before, inside], 1000)
    assert not bzm_fetch.page_is_older_than([], 1000)


def test_summary_kpis_normalization():
    row = {"hits": 200, "failed": 5, "avg": 100, "tp95": 300, "hits_avg": 10.0, "maxUsers": 5}
    kpis = bzm_fetch.summary_kpis(row)
    assert kpis["error_rate_pct"] == 2.5
    assert kpis["rps_per_vu"] == 2.0
    zero = bzm_fetch.summary_kpis({"hits": 0, "failed": 0})
    assert zero["error_rate_pct"] == 0.0
    assert zero["rps_per_vu"] is None


def test_compute_deltas_flags_adverse_moves():
    baseline = {"avg_ms": 200, "p95_ms": 480, "p99_ms": 700, "throughput_rps": 16.9,
                "rps_per_vu": 0.845, "error_rate_pct": 0.0, "max_users": 20}
    candidate = {"avg_ms": 240, "p95_ms": 642, "p99_ms": 800, "throughput_rps": 16.7,
                 "rps_per_vu": 0.835, "error_rate_pct": 0.4, "max_users": 20}
    deltas = bzm_fetch.compute_deltas(candidate, baseline)
    assert deltas["p95"]["pct"] == 33.8 and deltas["p95"]["adverse"]
    assert deltas["avg"]["pct"] == 20.0 and deltas["avg"]["adverse"]
    assert not deltas["throughput"]["adverse"]
    assert not deltas["throughput"]["normalized_per_vu"]
    # baseline error rate 0 -> judged in percentage points, not relative change
    assert deltas["error_rate"]["pct"] is None
    assert deltas["error_rate"]["points"] == 0.4
    assert not deltas["error_rate"]["adverse"]
    worst = bzm_fetch.worst_kpi_move(deltas)
    assert worst["kpi"] == "p95" and worst["pct"] == 33.8


def test_compute_deltas_normalizes_throughput_when_load_changed():
    baseline = {"throughput_rps": 20.0, "rps_per_vu": 1.0, "max_users": 20}
    # Half the load: raw RPS halves but per-VU throughput is unchanged - no regression.
    candidate = {"throughput_rps": 10.0, "rps_per_vu": 1.0, "max_users": 10}
    deltas = bzm_fetch.compute_deltas(candidate, baseline)
    assert deltas["throughput"]["normalized_per_vu"]
    assert deltas["throughput"]["pct"] == 0.0
    assert not deltas["throughput"]["adverse"]


def test_pick_candidate_prefers_newest_failing_run():
    runs = [
        {"id": 1, "ended": 100, "reportStatus": "fail"},
        {"id": 2, "ended": 200, "reportStatus": "pass"},
        {"id": 3, "ended": 150, "reportStatus": "fail"},
    ]
    assert bzm_fetch.pick_candidate(runs)["id"] == 3
    assert bzm_fetch.pick_candidate([runs[1]])["id"] == 2
    assert bzm_fetch.pick_candidate([]) is None


def test_fetch_summary_treats_zero_hit_all_row_as_unavailable():
    # GUI/EUX runs return an ALL row full of nulls; that is "no KPIs", not a
    # clean 0% error rate.
    transport = FakeTransport([
        {"path": "/masters/1/reports/default/summary",
         "response": {"result": {"summary": [{"id": "ALL", "lb": "ALL", "hits": None,
                                              "avg": None, "duration": 0}]}}},
    ])
    sweeper = bzm_fetch.Sweeper(transport, bzm_fetch.Coverage(), concurrency=1)
    assert sweeper.fetch_summary(1) is None


def test_anomaly_status_mapping():
    assert bzm_fetch.anomaly_status(None) == "statistics_unavailable"
    assert bzm_fetch.anomaly_status({"result": {}}) == "statistics_unavailable"
    assert bzm_fetch.anomaly_status({"result": {"anomalyCount": 0}}) == "no_anomalies"
    assert bzm_fetch.anomaly_status({"result": {"anomalyCount": 3}}) == "anomalies_with_details"


def test_anomaly_items_normalizes_both_live_shapes():
    # Seen live: `anomalies` is a list when empty, an anomalyId-keyed dict otherwise.
    row = {"kpi": "avg_rt", "labelName": "ALL"}
    assert bzm_fetch.anomaly_items({"result": {"anomalies": {"a1": row}}}) == [row]
    assert bzm_fetch.anomaly_items({"result": {"anomalies": [row]}}) == [row]
    assert bzm_fetch.anomaly_items({"result": {"anomalies": []}}) == []
    assert bzm_fetch.anomaly_items({"result": {}}) == []
    assert bzm_fetch.anomaly_items(None) == []


def test_health_for_covers_all_four_states():
    # unjudged: no KPI verdicts (only inconclusive/partial/running runs) — NEVER healthy.
    assert bzm_fetch.health_for(0, 0, 0, False) == "unjudged"
    assert bzm_fetch.health_for(0, 0, 0, True) == "unjudged"
    # critical: any failing run in the window...
    assert bzm_fetch.health_for(4, 3, 1, False) == "critical"
    # ...or SLA compliance (passed/kpi_runs) below 60%.
    assert bzm_fetch.health_for(10, 5, 0, False) == "critical"
    # at-risk: regressed while still green, or SLA below 90%.
    assert bzm_fetch.health_for(3, 3, 0, True) == "at-risk"
    assert bzm_fetch.health_for(10, 8, 0, False) == "at-risk"
    # healthy: all green, no regression; SLA exactly 90% is not "below 90%".
    assert bzm_fetch.health_for(3, 3, 0, False) == "healthy"
    assert bzm_fetch.health_for(10, 9, 0, False) == "healthy"


def test_worst_health_ordering_critical_beats_at_risk_beats_unjudged_beats_healthy():
    assert bzm_fetch.worst_health(["healthy"]) == "healthy"
    assert bzm_fetch.worst_health(["healthy", "unjudged"]) == "unjudged"
    assert bzm_fetch.worst_health(["unjudged", "at-risk", "healthy"]) == "at-risk"
    assert bzm_fetch.worst_health(["at-risk", "healthy", "critical", "unjudged"]) == "critical"
    assert bzm_fetch.worst_health([]) == "unjudged"  # nothing judged, never "healthy"


def test_worst_incident_ref_ranks_failures_first_then_magnitude():
    entries = [
        {"test_id": "1", "incident_candidates": [
            {"type": "anomaly", "execution_id": "10", "kpi": "p95"},
            {"type": "regression", "execution_id": "11",
             "worst_kpi_move": {"kpi": "p95", "pct": 40.0}},
        ]},
        {"test_id": "2", "incident_candidates": [{"type": "failure", "execution_id": "20"}]},
        {"test_id": "3", "incident_candidates": []},
    ]
    assert bzm_fetch.worst_incident_ref(entries) == {
        "type": "failure", "test_id": "2", "execution_id": "20",
    }
    # Without a failure, the regression outranks the anomaly.
    assert bzm_fetch.worst_incident_ref(entries[:1]) == {
        "type": "regression", "test_id": "1", "execution_id": "11",
    }
    assert bzm_fetch.worst_incident_ref([entries[2]]) is None
    # Within a class, the bigger move wins.
    regs = [
        {"test_id": "a", "incident_candidates": [
            {"type": "regression", "execution_id": "1",
             "worst_kpi_move": {"kpi": "avg", "pct": 12.0}}]},
        {"test_id": "b", "incident_candidates": [
            {"type": "regression", "execution_id": "2",
             "worst_kpi_move": {"kpi": "p95", "pct": 55.0}}]},
    ]
    assert bzm_fetch.worst_incident_ref(regs)["test_id"] == "b"


# --- credentials ------------------------------------------------------------------


def test_load_credentials_prefers_env_pair(tmp_path):
    assert bzm_fetch.load_credentials({"API_KEY_ID": "k", "API_KEY_SECRET": "s"}) == ("k", "s")


def test_load_credentials_reads_key_file(tmp_path):
    key = tmp_path / "api-key.json"
    key.write_text(json.dumps({"id": "kid", "secret": "ksec"}), encoding="utf-8")
    assert bzm_fetch.load_credentials({"BLAZEMETER_API_KEY": str(key)}) == ("kid", "ksec")


def test_load_credentials_errors_never_leak_the_secret(tmp_path):
    with pytest.raises(bzm_fetch.CredentialsError) as exc:
        bzm_fetch.load_credentials({})
    assert "API_KEY_ID" in str(exc.value)
    bad = tmp_path / "bad.json"
    bad.write_text("top-secret-value-not-json", encoding="utf-8")
    with pytest.raises(bzm_fetch.CredentialsError) as exc:
        bzm_fetch.load_credentials({"BLAZEMETER_API_KEY": str(bad)})
    assert "top-secret-value" not in str(exc.value)


# --- transport retry ---------------------------------------------------------------


def test_transport_retries_5xx_then_succeeds(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(req.full_url)
        if len(attempts) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "boom", {}, io.BytesIO(b""))
        return io.BytesIO(json.dumps({"result": []}).encode())

    monkeypatch.setattr(bzm_fetch.urllib.request, "urlopen", fake_urlopen)
    sleeps = []
    t = bzm_fetch.Transport("k", "s", sleep=sleeps.append)
    assert t.get("/user") == {"result": []}
    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]  # exponential backoff between attempts


def test_transport_does_not_retry_plain_4xx(monkeypatch):
    attempts = []

    def fake_urlopen(req, timeout=None):
        attempts.append(1)
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b""))

    monkeypatch.setattr(bzm_fetch.urllib.request, "urlopen", fake_urlopen)
    t = bzm_fetch.Transport("k", "s", sleep=lambda _: None)
    with pytest.raises(urllib.error.HTTPError):
        t.get("/accounts/1")
    assert len(attempts) == 1


# --- end-to-end sweep over the project fixture --------------------------------------


def run_project_sweep(tmp_path, **overrides):
    transport = FakeTransport.from_fixture("project_sweep.json")
    args = sweep_args(tmp_path, **overrides)
    rc = bzm_fetch.cmd_sweep(args, transport)
    digest = json.loads(Path(args.out).read_text(encoding="utf-8"))
    return rc, digest, transport


def test_sweep_produces_the_digest(tmp_path, capsys):
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(json.dumps({"201": "8000"}), encoding="utf-8")
    rc, digest, transport = run_project_sweep(tmp_path, baseline_file=str(baseline_file))

    assert rc == 0
    assert digest["schema_version"] == bzm_fetch.SCHEMA_VERSION
    assert digest["runs_in_window"] == 3 and digest["tests_ran"] == 2
    assert digest["coverage"]["http_failed"] == 0
    # Window-first: the catalog is never enumerated, only in-window activity.
    assert not any(path == "/tests" for path, _ in transport.calls)

    # Failures sort first: search-api (one failing run) ahead of checkout-flow.
    first, second = digest["tests"]
    assert first["test_id"] == "202"
    # test_name is the CANONICAL /tests name, not the run's (possibly custom) label.
    assert first["test_name"] == "search-api (canonical)"
    # 202's window already holds a non-candidate pass (9100), so no per-test
    # baseline-lookback listing is needed for it.
    assert not any(
        path == "/masters" and params.get("testId") == "202" for path, params in transport.calls
    )
    assert first["failed"] == 1 and first["passed"] == 1
    # v3: the workspace name comes from ONE cached /workspaces/{id} read.
    assert first["workspace"] == {"id": 11, "name": "Retail"}
    assert second["workspace"] == {"id": 11, "name": "Retail"}
    assert sum(1 for path, _ in transport.calls if path == "/workspaces/11") == 1
    # v3: per-test health chips — a failing run is critical; regressed-while-green at-risk.
    assert first["health"] == "critical"
    assert second["health"] == "at-risk"
    assert first["baseline"] == {"source": "last-passing", "execution_id": "9100"}
    assert first["candidate_execution_id"] == "9101"  # newest FAILING run wins
    assert {"type": "failure", "execution_id": "9101"} in first["incident_candidates"]
    spike = [c for c in first["incident_candidates"] if c["type"] == "error_spike"]
    assert spike and spike[0]["severe"] and spike[0]["error_rate_pct"] == 28.0
    assert first["anomaly_status"] == "anomalies_with_details"
    assert sum(1 for c in first["incident_candidates"] if c["type"] == "anomaly") == 2

    # checkout-flow: green but regressed vs its file-pinned baseline (p95 +33.8%).
    assert second["test_id"] == "201"
    assert second["baseline"] == {"source": "file", "execution_id": "8000"}
    assert second["regressed"]
    assert second["worst_kpi_move"]["kpi"] == "p95"
    assert second["worst_kpi_move"]["pct"] == 33.8
    endpoint = [c for c in second["incident_candidates"] if c["type"] == "endpoint_error_spike"]
    assert endpoint and endpoint[0]["label"] == "/checkout"
    assert endpoint[0]["errors_rate_pct"] == 98.3

    # stdout stays a tiny human summary, not data
    out = capsys.readouterr().out
    assert "across 2 tests" in out and "wrote" in out


def test_sweep_conversational_pin_beats_last_passing(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"202": 9100}), encoding="utf-8")
    rc, digest, _ = run_project_sweep(tmp_path, pins=str(pins))
    search = next(t for t in digest["tests"] if t["test_id"] == "202")
    assert search["baseline"] == {"source": "pin", "execution_id": "9100"}


def test_sweep_without_baseline_file_uses_prior_passing_run(tmp_path):
    rc, digest, _ = run_project_sweep(tmp_path)
    checkout = next(t for t in digest["tests"] if t["test_id"] == "201")
    # No file: last-passing excludes the candidate (9001) so the prior pass 8000
    # becomes the baseline - a green run is never its own baseline.
    assert checkout["baseline"] == {"source": "last-passing", "execution_id": "8000"}
    assert checkout["regressed"]
    assert checkout["worst_kpi_move"]["kpi"] == "p95"


def test_sweep_pin_pointing_at_candidate_yields_nothing_to_compare(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"201": 9001}), encoding="utf-8")
    rc, digest, _ = run_project_sweep(tmp_path, pins=str(pins))
    checkout = next(t for t in digest["tests"] if t["test_id"] == "201")
    assert "baseline_is_only_run" in checkout["notes"]
    assert not checkout["regressed"]


def test_sweep_rollups_aggregate_workspaces_and_projects(tmp_path):
    rc, digest, _ = run_project_sweep(tmp_path)
    assert rc == 0

    [ws] = digest["workspaces"]
    assert ws["id"] == 11 and ws["name"] == "Retail"
    assert ws["runs_in_window"] == 3 and ws["tests_ran"] == 2
    assert ws["passed"] == 2 and ws["failed"] == 1
    # Both tests regressed vs their own baselines (201 on p95, 202 on error rate).
    assert ws["regressed"] == 2 and ws["inconclusive"] == 0
    assert ws["health"] == "critical"  # worst member (202) wins over at-risk (201)
    assert ws["worst_incident"] == {
        "type": "failure", "test_id": "202", "execution_id": "9101",
    }

    [proj] = digest["projects"]
    assert proj["id"] == 101 and proj["name"] == "Checkout"
    assert proj["workspace"] == {"id": 11, "name": "Retail"}  # rows carry their workspace
    assert proj["health"] == "critical"
    assert proj["worst_incident"] == ws["worst_incident"]

    # Every rollup number sums from the member entries — nothing re-fetched or invented.
    for key in ("runs_in_window", "passed", "failed", "inconclusive"):
        assert ws[key] == sum(t[key] for t in digest["tests"])
        assert proj[key] == sum(t[key] for t in digest["tests"])
    assert ws["tests_ran"] == len(digest["tests"])
    assert ws["regressed"] == sum(1 for t in digest["tests"] if t["regressed"])


def test_sweep_workspace_name_failure_degrades_to_null_id_still_groups(tmp_path):
    transport = FakeTransport.from_fixture("project_sweep.json")
    transport.routes = [r for r in transport.routes if r["path"] != "/workspaces/11"]
    args = sweep_args(tmp_path)
    rc = bzm_fetch.cmd_sweep(args, transport)
    assert rc == 0  # names are cosmetic — degrade, never crash
    digest = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert all(t["workspace"] == {"id": 11, "name": None} for t in digest["tests"])
    [ws] = digest["workspaces"]
    assert ws["id"] == 11 and ws["name"] is None  # the id still groups
    assert digest["coverage"]["http_failed"] == 1
    assert any(
        f["stage"] == "workspace" and f["item"] == "workspace:11"
        for f in digest["coverage"]["failures"]
    )


def test_sweep_empty_window_reports_idle_not_fabricated(tmp_path):
    rc, digest, _ = run_project_sweep(tmp_path, from_="3000000", to="4000000")
    assert rc == 0
    assert digest["tests_ran"] == 0 and digest["runs_in_window"] == 0
    assert digest["tests"] == []
    assert digest["workspaces"] == [] and digest["projects"] == []  # nothing invented


def test_sweep_exceeding_max_failure_rate_exits_nonzero(tmp_path, capsys):
    transport = FakeTransport.from_fixture("project_sweep.json")
    # Drop every report route: per-run fetches fail, enumeration succeeds.
    transport.routes = [r for r in transport.routes if "/reports/" not in r["path"]
                        and "/anomalies/" not in r["path"]]
    args = sweep_args(tmp_path, max_failure_rate=0.1)
    rc = bzm_fetch.cmd_sweep(args, transport)
    assert rc == 3
    digest = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert digest["coverage"]["http_failed"] > 0
    assert digest["coverage"]["failures"]  # named failed items, honest coverage
    assert "exceeds --max-failure-rate" in capsys.readouterr().err


def test_plan_window_census_counts_runs_without_reports(tmp_path, capsys):
    transport = FakeTransport.from_fixture("project_sweep.json")
    args = argparse.Namespace(account_id=None, workspace_id=None, project_id="101",
                              concurrency=2, from_="1000000", to="2000000")
    rc = bzm_fetch.cmd_plan(args, transport)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["runs_in_window"] == 3 and plan["tests_ran"] == 2
    assert plan["per_test"][0]["runs"] == 2  # busiest test first
    assert plan["per_test"][0]["test_id"] == "202"  # string ids, matching the sweep/pins
    # Census is ONE windowed /masters listing: no catalog, no reports.
    assert not any(path == "/tests" for path, _ in transport.calls)
    assert not any("/reports/" in path for path, _ in transport.calls)


def _window_scope():
    return {"account_id": "9", "workspace_id": None, "project_id": None}


def _master(i, created):
    return {"id": i, "testId": 1, "projectId": 2, "name": "t", "created": created,
            "updated": created + 60, "ended": created + 60, "reportStatus": "pass"}


class PagedTransport:
    """Serves scripted /masters pages (or exceptions) in order."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = 0

    def get(self, path, params=None):
        self.calls += 1
        page = self.pages.pop(0)
        if isinstance(page, Exception):
            raise page
        return {"result": page, "error": None}


def test_masters_in_window_mid_listing_failure_degrades_to_partial():
    full_page = [_master(i, 1_500_000) for i in range(50)]
    transport = PagedTransport([full_page, urllib.error.URLError("boom")])
    cov = bzm_fetch.Coverage()
    sweeper = bzm_fetch.Sweeper(transport, cov, concurrency=1)
    masters = sweeper.masters_in_window(_window_scope(), 1_000_000, 2_000_000)
    # The page already fetched survives; the failure lands in coverage.
    assert len(masters) == 50
    assert cov.failed == 1 and cov.failures[0]["stage"] == "masters"


def test_masters_in_window_first_page_failure_is_scope_level():
    transport = PagedTransport([urllib.error.URLError("denied")])
    sweeper = bzm_fetch.Sweeper(transport, bzm_fetch.Coverage(), concurrency=1)
    with pytest.raises(urllib.error.URLError):
        sweeper.masters_in_window(_window_scope(), 1_000_000, 2_000_000)


def test_masters_in_window_stops_when_server_ignores_the_filter():
    # A server that ignores startTime/endTime returns full pages of ever-older
    # rows; the newest-first sort means a page entirely older than the window
    # must stop paging instead of walking the whole catalog history.
    in_window_page = [_master(i, 1_500_000) for i in range(50)]
    stale_page = [_master(100 + i, 500_000) for i in range(50)]
    transport = PagedTransport([in_window_page, stale_page, stale_page, stale_page])
    sweeper = bzm_fetch.Sweeper(transport, bzm_fetch.Coverage(), concurrency=1)
    masters = sweeper.masters_in_window(_window_scope(), 1_000_000, 2_000_000)
    assert len(masters) == 50  # stale rows filtered out
    assert transport.calls == 2  # stopped on the first fully-stale page


def test_masters_for_test_stop_when_ends_paging_early():
    page1 = [_master(1, 1_500_000)]  # a passing run on the first page
    transport = PagedTransport([page1 + [_master(i, 1_400_000) for i in range(49)],
                                [_master(60, 1_300_000) for _ in range(50)]])
    sweeper = bzm_fetch.Sweeper(transport, bzm_fetch.Coverage(), concurrency=1)
    got = sweeper.masters_for_test(
        1, 1_000_000, stop_when=lambda ms: any(m.get("reportStatus") == "pass" for m in ms)
    )
    assert transport.calls == 1  # satisfied after page one, no second fetch
    assert len(got) == 50


# --- run-pair over the pair fixture ---------------------------------------------------


def pair_args(tmp_path, **overrides) -> argparse.Namespace:
    defaults = dict(
        baseline_id="5001",
        candidate_id="5002",
        out=str(tmp_path / "compare.json"),
        no_anomalies=False,
        timeseries=False,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def run_pair(tmp_path, transport=None, **overrides):
    transport = transport or FakeTransport.from_fixture("run_pair.json")
    args = pair_args(tmp_path, **overrides)
    rc = bzm_fetch.cmd_run_pair(args, transport)
    compare = json.loads(Path(args.out).read_text(encoding="utf-8")) if Path(args.out).exists() else None
    return rc, compare, transport


def test_run_pair_produces_the_compare_json(tmp_path, capsys):
    rc, compare, _ = run_pair(tmp_path)
    assert rc == 0
    assert compare["schema_version"] == bzm_fetch.SCHEMA_VERSION
    assert compare["baseline"]["execution_id"] == "5001"
    assert compare["baseline"]["report_status"] == "pass"
    assert compare["candidate"]["report_status"] == "fail"
    assert compare["baseline"]["test_id"] == "201"
    assert compare["candidate"]["anomaly_status"] == "anomalies_with_details"
    assert compare["baseline"]["anomaly_status"] == "no_anomalies"

    v = compare["verdict_inputs"]
    assert v["candidate_failed_while_baseline_passed"]
    assert v["regressed"]
    assert v["load_config_differs"]  # 20 VU vs 10 VU
    assert compare["coverage"]["http_failed"] == 0

    out = capsys.readouterr().out
    assert out.count("\n") == 5  # five-line human summary
    assert "wrote" in out


def test_run_pair_normalizes_throughput_when_load_differs(tmp_path):
    # Candidate ran at half the load: raw RPS halves (20 -> 10) but per-VU
    # throughput is identical, so throughput must NOT read as a regression.
    rc, compare, _ = run_pair(tmp_path)
    tp = compare["kpi_deltas"]["throughput"]
    assert tp["normalized_per_vu"]
    assert tp["pct"] == 0.0
    assert not tp["adverse"]
    assert "throughput" not in compare["verdict_inputs"]["adverse_kpi_moves"]


def test_run_pair_error_rate_from_clean_baseline_is_judged_in_points(tmp_path):
    rc, compare, _ = run_pair(tmp_path)
    err = compare["kpi_deltas"]["error_rate"]
    assert err["pct"] is None  # relative change from 0% is undefined
    assert err["points"] == 26.6
    assert err["adverse"]


def test_run_pair_same_id_guard(tmp_path, capsys):
    transport = FakeTransport([])
    rc, compare, transport = run_pair(
        tmp_path, transport=transport, baseline_id="5001", candidate_id="5001"
    )
    assert rc == 2
    assert compare is None  # nothing written
    assert transport.calls == []  # guard fires before any fetch
    assert "compared to itself" in capsys.readouterr().err


def test_run_pair_missing_aggregate_report_degrades_into_coverage(tmp_path):
    transport = FakeTransport.from_fixture("run_pair.json")
    transport.routes = [r for r in transport.routes if "/aggregatereport/" not in r["path"]]
    rc, compare, _ = run_pair(tmp_path, transport=transport)
    assert rc == 0  # degrade, never crash
    assert compare["endpoints"]["matched"] == []
    assert "baseline_request_stats_unavailable" in compare["notes"]
    assert "candidate_request_stats_unavailable" in compare["notes"]
    assert compare["coverage"]["http_failed"] == 2
    # Run-level deltas still computed from the summaries.
    assert compare["kpi_deltas"]["p95"]["adverse"]


def test_run_pair_null_summary_run_reports_kpis_unavailable(tmp_path):
    # A GUI/EUX-style run returns an ALL row full of nulls: no load KPIs exist,
    # so the compare says "unavailable" — never a fabricated clean 0% error rate.
    rc, compare, _ = run_pair(tmp_path, candidate_id="5003")
    assert rc == 0
    assert compare["candidate"]["kpis"] is None
    assert "candidate_kpis_unavailable" in compare["notes"]
    assert compare["kpi_deltas"] == {}
    assert compare["verdict_inputs"]["worst_kpi_move"] is None
    assert not compare["verdict_inputs"]["regressed"]


def test_run_pair_endpoint_deltas_match_labels_across_runs(tmp_path):
    rc, compare, _ = run_pair(tmp_path)
    endpoints = compare["endpoints"]
    assert [e["label"] for e in endpoints["matched"]] == ["/checkout"]
    assert endpoints["baseline_only"] == ["/legacy"]
    assert endpoints["candidate_only"] == ["/new"]

    checkout = endpoints["matched"][0]
    assert checkout["deltas"]["avg"]["pct"] == 113.3
    assert checkout["deltas"]["avg"]["adverse"]
    assert checkout["deltas"]["error_rate"]["points"] == 33.25
    # Label throughput is judged per-VU too when the runs' load differs.
    assert checkout["deltas"]["throughput"]["normalized_per_vu"]
    assert not checkout["deltas"]["throughput"]["adverse"]
    assert "/checkout" in compare["verdict_inputs"]["endpoints_with_adverse_moves"]


def test_run_pair_unreadable_execution_errors_cleanly(tmp_path, capsys):
    transport = FakeTransport.from_fixture("run_pair.json")
    transport.routes = [r for r in transport.routes if r["path"] != "/masters/5002"]
    rc, compare, _ = run_pair(tmp_path, transport=transport)
    assert rc == 3
    assert compare is None
    assert "could not read execution 5002" in capsys.readouterr().err


# --- end-to-end history over the single-test fixture --------------------------------


def history_args(tmp_path, **overrides) -> argparse.Namespace:
    defaults = dict(
        test_id="301",
        from_="1000000",
        to="2000000",
        out=str(tmp_path / "history.json"),
        baseline_file=None,
        pins=None,
        concurrency=2,
        max_failure_rate=0.2,
        no_anomalies=False,
        timeseries=False,
        curve_runs=bzm_fetch.DEFAULT_CURVE_RUNS,
    )
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def run_history(tmp_path, **overrides):
    transport = FakeTransport.from_fixture("test_history.json")
    args = history_args(tmp_path, **overrides)
    rc = bzm_fetch.cmd_history(args, transport)
    history = json.loads(Path(args.out).read_text(encoding="utf-8"))
    return rc, history, transport


def test_history_produces_the_per_run_series(tmp_path, capsys):
    rc, history, transport = run_history(tmp_path)

    assert rc == 0
    assert history["schema_version"] == bzm_fetch.SCHEMA_VERSION
    assert history["test_id"] == "301" and history["test_name"] == "checkout-flow"

    # Window edges: 9000 ends exactly at --from (included); 8000 predates it (excluded).
    assert [r["execution_id"] for r in history["runs"]] == [
        "9000", "9051", "9101", "9201", "9301", "9401",
    ]  # chronological, oldest first — the trend axis

    # Status buckets: pass/fail carry KPIs; abort is skipped-partial; noData inconclusive.
    buckets = {r["execution_id"]: r["bucket"] for r in history["runs"]}
    assert buckets["9401"] == "kpi" and buckets["9301"] == "kpi"
    assert buckets["9101"] == "partial" and buckets["9051"] == "inconclusive"
    assert history["runs_in_window"] == 6 and history["kpi_runs"] == 4
    assert history["passed"] == 3 and history["failed"] == 1
    assert history["skipped_partial"] == 1 and history["inconclusive"] == 1

    # No report fetches for partial/inconclusive/out-of-window runs.
    fetched = [path for path, _ in transport.calls if "/reports/" in path]
    assert not any(m in p for m in ("9101", "9051", "8000") for p in fetched)

    # Baseline: last-passing excludes the candidate (9301, the newest failing run).
    assert history["candidate_execution_id"] == "9301"
    assert history["baseline"] == {"source": "last-passing", "execution_id": "9401"}
    assert history["baseline_kpis"]["avg_ms"] == 210

    runs = {r["execution_id"]: r for r in history["runs"]}
    # The baseline run is marked and never compared to itself.
    assert runs["9401"]["is_baseline"] and runs["9401"]["deltas"] == {}
    # The failing run's deltas vs the baseline, plus its per-run incidents.
    assert runs["9301"]["deltas"]["avg"] == {"pct": 42.9, "adverse": True}
    assert runs["9301"]["deltas"]["p95"]["pct"] == 40.0
    assert runs["9301"]["regressed"] and runs["9301"]["worst_kpi_move"]["kpi"] == "avg"
    assert runs["9301"]["anomaly_status"] == "anomalies_with_details"
    assert runs["9301"]["anomalies"] == [{"kpi": "p95", "label": "/search"}]
    # A near-identical green run gets deltas but no adverse flags.
    assert runs["9201"]["deltas"] and not runs["9201"]["regressed"]
    # Zero-hit summary row (GUI/EUX-style) -> KPIs unavailable, never fabricated.
    assert runs["9000"]["kpis"] is None and "kpis_unavailable" in runs["9000"]["notes"]
    assert runs["9000"]["deltas"] == {}
    assert runs["9000"]["anomaly_status"] == "statistics_unavailable"

    types = [c["type"] for c in history["incident_candidates"]]
    assert sorted(types) == ["endpoint_error_spike", "error_spike", "failure", "regression"]
    spike = next(c for c in history["incident_candidates"] if c["type"] == "error_spike")
    assert spike["severe"] and spike["error_rate_pct"] == 26.0
    endpoint = next(c for c in history["incident_candidates"] if c["type"] == "endpoint_error_spike")
    assert endpoint["label"] == "/search" and endpoint["errors_rate_pct"] == 98.0
    assert history["regressed_runs"] == 1

    out = capsys.readouterr().out
    assert "6 runs in window" in out and "wrote" in out


def test_history_baseline_precedence_pin_beats_file(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"301": 9201}), encoding="utf-8")
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(json.dumps({"301": "9401"}), encoding="utf-8")

    rc, history, _ = run_history(tmp_path, pins=str(pins), baseline_file=str(baseline_file))
    assert history["baseline"] == {"source": "pin", "execution_id": "9201"}
    # Deltas are re-anchored on the pinned run.
    runs = {r["execution_id"]: r for r in history["runs"]}
    assert runs["9201"]["is_baseline"] and runs["9201"]["deltas"] == {}
    assert runs["9401"]["deltas"]["avg"]["pct"] == 2.4

    rc, history, _ = run_history(tmp_path, baseline_file=str(baseline_file))
    assert history["baseline"] == {"source": "file", "execution_id": "9401"}


def test_history_pin_at_candidate_notes_nothing_to_compare(tmp_path):
    pins = tmp_path / "pins.json"
    pins.write_text(json.dumps({"301": 9301}), encoding="utf-8")
    rc, history, _ = run_history(tmp_path, pins=str(pins))
    assert history["baseline"] == {"source": "pin", "execution_id": "9301"}
    assert "baseline_is_only_run" in history["notes"]
    runs = {r["execution_id"]: r for r in history["runs"]}
    # The candidate is its own baseline: no self-comparison, no fabricated 0% move.
    assert runs["9301"]["is_baseline"] and runs["9301"]["deltas"] == {}
    assert not runs["9301"]["regressed"]
    # Other runs still compare against the pinned run.
    assert runs["9401"]["deltas"] and not runs["9401"]["regressed"]


def test_history_empty_window_reports_no_runs(tmp_path, capsys):
    rc, history, _ = run_history(tmp_path, from_="3000000", to="4000000")
    assert rc == 0
    assert history["runs_in_window"] == 0 and history["runs"] == []
    assert history["candidate_execution_id"] is None
    assert history["incident_candidates"] == []
    assert "0 runs in window" in capsys.readouterr().out


def test_history_rejects_inverted_window(tmp_path, capsys):
    transport = FakeTransport.from_fixture("test_history.json")
    rc = bzm_fetch.cmd_history(history_args(tmp_path, from_="2000000", to="1000000"), transport)
    assert rc == 2
    assert "--from must be earlier than --to" in capsys.readouterr().err


def test_history_exceeding_max_failure_rate_exits_nonzero(tmp_path, capsys):
    transport = FakeTransport.from_fixture("test_history.json")
    transport.routes = [r for r in transport.routes if "/reports/" not in r["path"]
                        and "/anomalies/" not in r["path"]]
    args = history_args(tmp_path, max_failure_rate=0.1)
    rc = bzm_fetch.cmd_history(args, transport)
    assert rc == 3
    history = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert history["coverage"]["http_failed"] > 0
    assert history["coverage"]["failures"]  # named failed items, honest coverage
    assert "baseline_kpis_unavailable" in history["notes"]
    runs = {r["execution_id"]: r for r in history["runs"]}
    assert all(r["kpis"] is None for r in runs.values() if r["bucket"] == "kpi")
    assert "exceeds --max-failure-rate" in capsys.readouterr().err


# --- intra-run timeseries (--timeseries) ---------------------------------------------


def minute_points(start=1000, **cols):
    """Build kpi-values datapoints from parallel column lists (60s buckets)."""
    length = len(next(iter(cols.values())))
    return [
        {"ts": start + 60 * i, **{k: v[i] for k, v in cols.items() if v[i] is not None}}
        for i in range(length)
    ]


def test_all_label_id_matches_both_naming_conventions():
    assert bzm_fetch.all_label_id({"result": [{"id": "a1", "name": "ALL"}]}) == "a1"
    assert bzm_fetch.all_label_id({"result": [{"labelId": "b2", "labelName": "ALL"}]}) == "b2"
    # No exact-ALL row -> None, never a guess at some other label.
    assert bzm_fetch.all_label_id({"result": [{"id": "c3", "name": "/checkout"}]}) is None
    assert bzm_fetch.all_label_id({"result": []}) is None
    assert bzm_fetch.all_label_id(None) is None


def test_series_points_finds_the_datapoint_list_and_sorts_by_ts():
    payload = {
        "result": [
            {
                "labelId": "a1",
                "labelName": "ALL",
                "kpis": [{"ts": 120, "n": 2}, {"ts": 60, "n": 1}, {"n": 99}],
            }
        ]
    }
    points = bzm_fetch.series_points(payload)
    assert [p["ts"] for p in points] == [60, 120]  # sorted; the ts-less row dropped
    assert bzm_fetch.series_points({"result": []}) == []
    assert bzm_fetch.series_points(None) == []


def test_downsample_curve_merges_buckets_with_honest_units():
    # 130 minute buckets -> stride 3 -> 44 merged columns.
    points = minute_points(
        start=0,
        na=list(range(130)),
        n=[60] * 130,
        ec=[0] * 130,
        t_avg=[100 if i != 1 else 400 for i in range(130)],
        t_pec95=[300 if i != 4 else 500 for i in range(130)],
    )
    curve = bzm_fetch.downsample_curve(points, 60, max_points=60)
    assert len(curve["offset_s"]) == 44
    assert curve["offset_s"][:3] == [0, 180, 360]
    assert curve["users"][0] == 2  # gauge: peak of the merged span
    assert curve["hits_per_s"][0] == 1.0  # counts: summed over the span, then a rate
    assert curve["avg_ms"][0] == 200.0  # hits-weighted mean of (100, 400, 100)
    assert curve["p95_ms"][1] == 500  # percentile: the span's WORST bucket
    assert curve["hits_per_s"][-1] == 1.0  # last group is a single bucket, span honest


def test_downsample_curve_keeps_gaps_null_not_zero():
    points = [{"ts": 0, "n": 60, "t_avg": 100}, {"ts": 60}]
    curve = bzm_fetch.downsample_curve(points, 60, max_points=60)
    assert curve["users"] == [None, None]
    assert curve["error_rate_pct"][0] == 0.0  # hits with zero errors IS a clean 0%
    assert curve["error_rate_pct"][1] is None  # no hits -> unknown, never 0
    assert curve["avg_ms"][1] is None and curve["p95_ms"][1] is None


def test_shape_summary_ramp_phases_slope_and_saturation():
    points = minute_points(
        na=[5, 10, 15, 20, 20, 20, 20, 20, 20, 20],
        n=[25, 50, 75, 100, 100, 100, 100, 100, 100, 100],
        ec=[0] * 10,
        t_avg=[200, 205, 210, 205, 208, 210, 207, 209, 210, 208],
        t_pec95=[380, 390, 395, 400, 405, 410, 415, 420, 425, 430],
    )
    shape = bzm_fetch.shape_summary(points, 60)
    assert shape["peak_users"] == 20
    assert shape["time_to_peak_users_s"] == 180  # first bucket within 5% of peak
    assert shape["phases"]["ramp"]["buckets"] == 3
    assert shape["phases"]["steady_early"]["buckets"] == 3
    assert shape["phases"]["steady_late"]["buckets"] == 4
    assert shape["phases"]["ramp"]["avg_ms"] == 206.7  # hits-weighted
    assert shape["p95_slope_ms_per_min"] == 5.0  # exact: p95 climbs 5 ms per steady minute
    assert shape["rt_spikes"] == [] and shape["error_bursts"] == []
    # Throughput peaks exactly when users do -> no saturation knee.
    assert shape["saturation"]["throughput_peak_offset_s"] == 180
    assert not shape["saturation"]["throughput_peaked_before_users"]


def test_shape_summary_flags_spike_burst_and_saturation_knee():
    points = minute_points(
        na=[5, 10, 15, 20, 20, 20, 20, 20, 20, 20],
        n=[100] * 10,  # throughput flat from minute 0 while users still ramp: a knee
        ec=[0, 0, 0, 0, 0, 50, 0, 0, 0, 0],
        t_avg=[230, 235, 240, 238, 242, 600, 245, 240, 238, 236],
        t_pec95=[460, 470, 480, 475, 485, 1200, 490, 480, 476, 472],
    )
    shape = bzm_fetch.shape_summary(points, 60)
    assert [s["offset_s"] for s in shape["rt_spikes"]] == [300]  # >= 2x run median
    assert shape["rt_spikes"][0]["avg_ms"] == 600 and shape["rt_spikes"][0]["p95_ms"] == 1200
    assert shape["error_bursts"] == [{"offset_s": 300, "error_rate_pct": 50.0}]
    assert shape["saturation"]["throughput_peaked_before_users"]


def test_shape_summary_short_run_degrades_gracefully():
    # A 1-minute validation run: one bucket, no ramp, no slope, nothing to split.
    points = minute_points(na=[1], n=[10], ec=[0], t_avg=[100], t_pec95=[200])
    shape = bzm_fetch.shape_summary(points, 60)
    assert shape["phases"]["ramp"] is None
    assert shape["phases"]["steady_early"]["buckets"] == 1
    assert shape["phases"]["steady_late"] is None
    assert shape["p95_slope_ms_per_min"] is None
    assert bzm_fetch.build_timeseries([]) is None


def test_history_timeseries_caps_curves_and_forces_candidate_and_baseline(tmp_path, capsys):
    # curve-runs=1 selects only the newest KPI run (9401, also the baseline);
    # the candidate (9301, newest failing) is force-included. 9201/9000 stay
    # curve-free — fetching them would be a fixture error.
    rc, history, transport = run_history(tmp_path, timeseries=True, curve_runs=1)
    assert rc == 0
    runs = {r["execution_id"]: r for r in history["runs"]}

    ts = runs["9401"]["timeseries"]
    assert ts["interval_s"] == 60 and ts["buckets"] == 10
    assert ts["start_ts"] == 1700000
    assert len(ts["curve"]["offset_s"]) == 10  # under the cap: no downsampling
    assert ts["curve"]["users"][-1] == 20
    assert ts["shape"]["p95_slope_ms_per_min"] == 5.0
    assert ts["shape"]["rt_spikes"] == []

    ts = runs["9301"]["timeseries"]  # labelId/labelName + `values` container variant
    assert [s["offset_s"] for s in ts["shape"]["rt_spikes"]] == [300]
    assert ts["shape"]["error_bursts"][0]["error_rate_pct"] == 50.0
    assert ts["shape"]["saturation"]["throughput_peaked_before_users"]

    assert "timeseries" not in runs["9201"] and "timeseries" not in runs["9000"]
    assert history["coverage"]["timeseries_unavailable"] == 0
    assert "timeseries: curves for 2/2 selected runs (interval 60s)" in capsys.readouterr().out

    # Exactly two curve pulls happened: one labels + one kpi-values per selected run.
    assert sum(1 for p, _ in transport.calls if p == "/data/labels") == 2
    assert sum(1 for p, _ in transport.calls if p.endswith("/kpi-values")) == 2
    kpi_value_params = [q for p, q in transport.calls if p.endswith("/kpi-values")]
    assert all(q == {"id": "label/a3f0/t/pec95", "interval": "60"} for q in kpi_value_params)


def test_history_timeseries_pull_failure_degrades_to_note(tmp_path):
    transport = FakeTransport.from_fixture("test_history.json")
    transport.routes = [r for r in transport.routes if r["path"] != "/masters/9301/kpi-values"]
    args = history_args(tmp_path, timeseries=True, curve_runs=1, max_failure_rate=1.0)
    rc = bzm_fetch.cmd_history(args, transport)
    history = json.loads(Path(args.out).read_text(encoding="utf-8"))
    assert rc == 0
    runs = {r["execution_id"]: r for r in history["runs"]}
    assert runs["9401"]["timeseries"] is not None
    assert runs["9301"]["timeseries"] is None
    assert "timeseries_unavailable" in runs["9301"]["notes"]
    assert history["coverage"]["timeseries_unavailable"] == 1
    assert any(f["stage"] == "timeseries" for f in history["coverage"]["failures"])


def test_history_without_timeseries_flag_pulls_no_curves(tmp_path):
    rc, history, transport = run_history(tmp_path)
    assert rc == 0
    assert not any(p == "/data/labels" or p.endswith("/kpi-values") for p, _ in transport.calls)
    assert all("timeseries" not in r for r in history["runs"])
    assert "timeseries_unavailable" not in history["coverage"]


def test_run_pair_timeseries_attaches_both_sides(tmp_path):
    rc, compare, _ = run_pair(tmp_path, timeseries=True)
    assert rc == 0
    assert compare["baseline"]["timeseries"]["shape"]["peak_users"] == 20
    assert compare["candidate"]["timeseries"]["shape"]["peak_users"] == 10
    assert len(compare["baseline"]["timeseries"]["curve"]["offset_s"]) == 6
    assert not any("timeseries_unavailable" in n for n in compare["notes"])


def test_run_pair_timeseries_missing_labels_degrades_to_notes(tmp_path):
    transport = FakeTransport.from_fixture("run_pair.json")
    transport.routes = [r for r in transport.routes if r["path"] != "/data/labels"]
    rc, compare, _ = run_pair(tmp_path, transport=transport, timeseries=True)
    assert rc == 0  # curves degrade, never fail the compare
    assert compare["baseline"]["timeseries"] is None
    assert compare["candidate"]["timeseries"] is None
    assert "baseline_timeseries_unavailable" in compare["notes"]
    assert "candidate_timeseries_unavailable" in compare["notes"]
    # The KPI compare itself is untouched.
    assert compare["kpi_deltas"]["p95"]["adverse"]


# --- live drift tripwire (auto-skips without credentials) ----------------------------

_HAVE_CREDS = bool(
    (os.environ.get("API_KEY_ID") and os.environ.get("API_KEY_SECRET"))
    or (os.environ.get("BLAZEMETER_API_KEY") and Path(os.environ["BLAZEMETER_API_KEY"]).exists())
)


@pytest.mark.live
@pytest.mark.skipif(not _HAVE_CREDS, reason="no BlazeMeter credentials in the environment")
def test_live_user_and_envelope_shape():
    key_id, key_secret = bzm_fetch.load_credentials()
    t = bzm_fetch.Transport(key_id, key_secret)
    user = t.get("/user")
    assert isinstance(user.get("result"), dict) and user["result"].get("id")
    accounts = t.get("/accounts", {"limit": 1, "skip": 0})
    assert isinstance(accounts.get("result"), list)
    assert "total" in accounts or accounts["result"]  # envelope contract from API_NOTES.md


@pytest.mark.live
@pytest.mark.skipif(not _HAVE_CREDS, reason="no BlazeMeter credentials in the environment")
def test_live_intra_run_timeseries_endpoints():
    """Drift tripwire for the two --timeseries endpoints (/data/labels, /kpi-values).

    Pins the doc-derived assumptions the fixtures encode: the labels listing is a
    flat id/name list containing an aggregate ALL row, and one kpi-values series at
    interval=60 returns datapoints carrying `ts` plus the multi-KPI field set
    (na/n/t_avg/t_pec95). Skips (not fails) when the account has no finished run —
    absence of data is not endpoint drift.
    """
    key_id, key_secret = bzm_fetch.load_credentials()
    t = bzm_fetch.Transport(key_id, key_secret)
    account_id = (t.get("/user")["result"].get("defaultProject") or {}).get("accountId")
    if not account_id:
        pytest.skip("user has no default account to search for a finished run")
    masters = t.get(
        "/masters", {"accountId": account_id, "limit": 20, "skip": 0, "sort[]": "-created"}
    ).get("result") or []
    ended = [m for m in masters if m.get("ended") and m.get("reportStatus") in ("pass", "fail")]
    if not ended:
        pytest.skip("no finished KPI-bearing run in the account's recent history")
    master_id = str(ended[0]["id"])

    labels = t.get("/data/labels", {"master_id": master_id})
    assert isinstance(labels.get("result"), list)
    label_id = bzm_fetch.all_label_id(labels)
    assert label_id, "no aggregate ALL row in /data/labels — fixtures assume one exists"

    series = t.get(
        "/masters/%s/kpi-values" % master_id,
        {"id": "label/%s/t/pec95" % label_id, "interval": bzm_fetch.TIMESERIES_INTERVAL_S},
    )
    points = bzm_fetch.series_points(series)
    assert points, "kpi-values returned no ts-bearing datapoints"
    assert all(isinstance(p.get("ts"), (int, float)) for p in points)
    # The multi-KPI field set the curves are built from (t_pec95 was the requested id).
    sample = {k for p in points for k in p}
    assert {"na", "n", "t_avg", "t_pec95"} & sample, "datapoints lack the multi-KPI fields"
    block = bzm_fetch.build_timeseries(points)
    assert block and len(block["curve"]["offset_s"]) <= bzm_fetch.MAX_CURVE_POINTS
