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
    assert digest["tests_in_scope"] == 2 and digest["tests_ran"] == 2
    assert digest["coverage"]["http_failed"] == 0

    # Failures sort first: search-api (one failing run) ahead of checkout-flow.
    first, second = digest["tests"]
    assert first["test_id"] == "202"
    assert first["failed"] == 1 and first["passed"] == 1
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
    assert "2 ran in window" in out and "wrote" in out


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


def test_sweep_empty_window_reports_idle_not_fabricated(tmp_path):
    rc, digest, _ = run_project_sweep(tmp_path, from_="3000000", to="4000000")
    assert rc == 0
    assert digest["tests_ran"] == 0 and digest["idle_tests"] == 2
    assert digest["tests"] == []


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


def test_plan_census_counts_without_fetching_reports(tmp_path, capsys):
    transport = FakeTransport.from_fixture("project_sweep.json")
    args = argparse.Namespace(account_id=None, workspace_id=None, project_id="101",
                              concurrency=2)
    rc = bzm_fetch.cmd_plan(args, transport)
    assert rc == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["tests"] == 2 and plan["projects"] == 1
    assert not any("/masters" in path for path, _ in transport.calls)


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
