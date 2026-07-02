"""Tests for the InfluxDB 2.x client (`shared/scripts/bzm_influx.py`).

All HTTP goes through the `Transport.post` seam; these tests inject a fake
transport (canned response/exception sequences), so nothing here touches the
network. The `live`-marked test at the bottom round-trips two points against a
real InfluxDB 2.x (e.g. a local `influxdb:2` container) and auto-skips when
INFLUX_URL/INFLUX_TOKEN are absent from the environment (so CI stays green).
"""

from __future__ import annotations

import email.message
import gzip
import io
import json
import os
import time
import urllib.error
from pathlib import Path

import pytest

import bzm_influx

FIXTURES = Path(__file__).parent / "fixtures" / "bzm_influx"

TOKEN = "sekret-token-value"


class FakeTransport:
    """Serves a scripted sequence of outcomes: bytes to return, or exceptions to raise.

    Records every call (path, params, body, headers) so tests can assert on the
    exact request shape. Running past the script fails loudly.
    """

    def __init__(self, outcomes: list | None = None):
        self.outcomes = list(outcomes or [])
        self.calls: list[tuple[str, dict, bytes, dict]] = []

    def post(self, path: str, params: dict | None, body: bytes, headers: dict) -> bytes:
        self.calls.append((path, dict(params or {}), body, dict(headers or {})))
        if not self.outcomes:
            raise AssertionError("unscripted POST %s" % path)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    headers = email.message.Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError(
        "http://influx.local:8086/api/v2/write", code, "boom", headers, io.BytesIO(b"")
    )


@pytest.fixture
def influx_env(monkeypatch):
    monkeypatch.setenv("INFLUX_URL", "http://influx.local:8086")
    monkeypatch.setenv("INFLUX_TOKEN", TOKEN)
    monkeypatch.setenv("INFLUX_ORG", "perforce")
    monkeypatch.setenv("INFLUX_BUCKET", "bzm")


def make_writer(monkeypatch, fake: FakeTransport, **kwargs) -> bzm_influx.InfluxWriter:
    """An InfluxWriter wired to the fake transport, with sleeps recorded not slept."""
    monkeypatch.setattr(bzm_influx, "Transport", lambda *a, **k: fake)
    writer = bzm_influx.InfluxWriter(**kwargs)
    writer.sleeps = []
    writer._sleep = writer.sleeps.append
    return writer


# --- write: batching, gzip, request shape -----------------------------------------


def test_write_splits_batches_at_5000_lines(monkeypatch, influx_env):
    fake = FakeTransport([b"", b""])
    writer = make_writer(monkeypatch, fake)
    lines = ["m,t=1 v=%di %d" % (i, i) for i in range(5001)]
    stats = writer.write(lines)
    assert [len(gzip.decompress(body).decode().splitlines()) for _, _, body, _ in fake.calls] == [
        5000,
        1,
    ]
    assert stats == bzm_influx.WriteStats(
        attempted_lines=5001, written_lines=5001, failed_batches=0, retries=0
    )


def test_write_request_shape_gzip_body_and_precision_seconds(monkeypatch, influx_env):
    fake = FakeTransport([b""])
    writer = make_writer(monkeypatch, fake)
    lines = ["bzm_run,test_id=7 avg_ms=12.5 1782901800", "bzm_run,test_id=8 avg_ms=9 1782901860"]
    writer.write(lines)
    path, params, body, headers = fake.calls[0]
    assert path == "/api/v2/write"
    assert params == {"org": "perforce", "bucket": "bzm", "precision": "s"}
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Content-Type"].startswith("text/plain")
    assert gzip.decompress(body).decode("utf-8") == "\n".join(lines)


def test_write_bucket_override_and_points_bucket_routing(monkeypatch, influx_env):
    fake = FakeTransport([b"", b""])
    writer = make_writer(monkeypatch, fake, bucket="runs", points_bucket="points-30d")
    assert writer.bucket == "runs" and writer.points_bucket == "points-30d"
    writer.write(["a v=1i 1"])
    writer.write(["b v=1i 2"], bucket=writer.points_bucket)
    assert [params["bucket"] for _, params, _, _ in fake.calls] == ["runs", "points-30d"]


def test_points_bucket_falls_back_to_main_bucket(monkeypatch, influx_env):
    writer = make_writer(monkeypatch, FakeTransport())
    assert writer.bucket == "bzm" and writer.points_bucket == "bzm"


def test_write_empty_lines_never_touches_the_network(monkeypatch, influx_env):
    fake = FakeTransport()
    writer = make_writer(monkeypatch, fake)
    assert writer.write([]) == bzm_influx.WriteStats()
    assert fake.calls == []


# --- write: retry/backoff and failed-batch continuation ----------------------------


def test_write_retries_429_honoring_retry_after(monkeypatch, influx_env):
    fake = FakeTransport([http_error(429, retry_after="7"), b""])
    writer = make_writer(monkeypatch, fake)
    stats = writer.write(["m v=1i 1"])
    assert writer.sleeps == [7.0]  # Retry-After wins over exponential backoff
    assert stats == bzm_influx.WriteStats(
        attempted_lines=1, written_lines=1, failed_batches=0, retries=1
    )


def test_write_retries_5xx_with_exponential_backoff(monkeypatch, influx_env):
    fake = FakeTransport([http_error(500), http_error(503), b""])
    writer = make_writer(monkeypatch, fake)
    stats = writer.write(["m v=1i 1"])
    assert writer.sleeps == [1.0, 2.0]
    assert stats.retries == 2 and stats.written_lines == 1 and stats.failed_batches == 0


def test_write_failed_batch_is_recorded_and_the_rest_continues(monkeypatch, influx_env):
    # Batch 1 exhausts all MAX_ATTEMPTS; batch 2 succeeds. The retries spent on
    # the doomed batch still count — the stats never launder a partial push.
    fake = FakeTransport([http_error(500), http_error(500), http_error(500), b""])
    writer = make_writer(monkeypatch, fake)
    lines = ["m,t=%d v=1i %d" % (i, i) for i in range(5001)]
    stats = writer.write(lines)
    assert stats == bzm_influx.WriteStats(
        attempted_lines=5001, written_lines=1, failed_batches=1, retries=2
    )
    assert len(fake.calls) == 4  # 3 attempts on batch 1, then batch 2


def test_write_client_error_fails_the_batch_without_retrying(monkeypatch, influx_env):
    # A 400 (malformed line protocol) will not improve with retries.
    fake = FakeTransport([http_error(400), b""])
    writer = make_writer(monkeypatch, fake)
    stats = writer.write(["m v=1i 1", "m v=1i 2"], bucket="bzm")
    stats2 = writer.write(["m v=1i 3"])
    assert writer.sleeps == []
    assert stats.failed_batches == 1 and stats.written_lines == 0 and stats.retries == 0
    assert stats2.written_lines == 1


# --- watermark query ----------------------------------------------------------------


def watermark_call(monkeypatch, csv_name: str, **kwargs):
    fake = FakeTransport([(FIXTURES / csv_name).read_bytes()])
    monkeypatch.setattr(bzm_influx, "Transport", lambda *a, **k: fake)
    result = bzm_influx.query_watermark(**kwargs)
    return result, fake


def test_query_watermark_flux_body_and_result(monkeypatch, influx_env):
    result, fake = watermark_call(
        monkeypatch,
        "watermark.csv",
        measurement="bzm_run",
        tag_filters={"project_id": "101", "account_id": "9"},
    )
    assert result == 1782901800  # 2026-07-01T10:30:00Z
    path, params, body, headers = fake.calls[0]
    assert path == "/api/v2/query" and params == {"org": "perforce"}
    assert headers == {"Content-Type": "application/json", "Accept": "application/csv"}
    payload = json.loads(body.decode("utf-8"))
    assert payload["type"] == "flux"
    flux = payload["query"]
    assert 'from(bucket: "bzm")' in flux
    assert 'r._measurement == "bzm_run"' in flux
    # Tag equalities are emitted sorted, ANDed inside one filter.
    assert 'r["account_id"] == "9" and r["project_id"] == "101"' in flux
    assert '|> last(column: "_time")' in flux


def test_query_watermark_none_when_no_matching_series(monkeypatch, influx_env):
    result, _ = watermark_call(
        monkeypatch, "watermark_empty.csv", measurement="bzm_run", tag_filters={}
    )
    assert result is None


def test_watermark_from_csv_takes_newest_across_tables_and_drops_fractions():
    text = (FIXTURES / "watermark_multi_table.csv").read_text(encoding="utf-8")
    assert bzm_influx.watermark_from_csv(text) == 1782980100  # 2026-07-02T08:15:00Z
    assert bzm_influx.watermark_from_csv("") is None


def test_watermark_from_csv_raises_on_flux_error_table():
    # Influx can answer HTTP 200 with an error table (error,reference columns);
    # sync must see a failure, never "no data" (which would silently fall back).
    text = (
        "#datatype,string,long\n"
        "#group,true,true\n"
        "#default,,\n"
        ",error,reference\n"
        ',compilation failed: loc 1:1,"897"\n'
    )
    with pytest.raises(bzm_influx.InfluxQueryError, match="compilation failed"):
        bzm_influx.watermark_from_csv(text)


def test_build_watermark_flux_escapes_quotes_and_skips_empty_filter():
    flux = bzm_influx.build_watermark_flux(
        bucket='b"kt', measurement="bzm_run", tag_filters={"name": 'say "hi"\\'}
    )
    assert 'from(bucket: "b\\"kt")' in flux
    assert 'r["name"] == "say \\"hi\\"\\\\"' in flux
    no_tags = bzm_influx.build_watermark_flux(bucket="b", measurement="m", tag_filters={})
    assert no_tags.count("|> filter") == 1  # only the measurement filter


# --- configuration and redaction ----------------------------------------------------


def test_missing_env_var_error_names_the_variable(monkeypatch, influx_env):
    monkeypatch.delenv("INFLUX_URL")
    with pytest.raises(bzm_influx.InfluxConfigError) as excinfo:
        bzm_influx.InfluxWriter()
    assert "INFLUX_URL" in str(excinfo.value)
    monkeypatch.delenv("INFLUX_BUCKET")
    with pytest.raises(bzm_influx.InfluxConfigError) as excinfo:
        bzm_influx.query_watermark(measurement="bzm_run", tag_filters={})
    assert str(excinfo.value)  # names variables only — never any value


def test_token_never_in_repr_or_error_text(monkeypatch, influx_env):
    transport = bzm_influx.Transport("http://influx.local:8086", TOKEN)
    assert TOKEN not in repr(transport)
    fake = FakeTransport([http_error(401)])
    writer = make_writer(monkeypatch, fake)
    assert TOKEN not in repr(writer)
    monkeypatch.setattr(bzm_influx, "Transport", lambda *a, **k: FakeTransport([http_error(401)]))
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        bzm_influx.query_watermark(measurement="bzm_run", tag_filters={})
    assert TOKEN not in str(excinfo.value) and TOKEN not in repr(excinfo.value)
    with pytest.raises(bzm_influx.InfluxConfigError) as config_err:
        monkeypatch.delenv("INFLUX_ORG")
        bzm_influx.InfluxWriter()
    assert TOKEN not in str(config_err.value)


# --- CLI ------------------------------------------------------------------------------


def test_cli_help_smokes():
    with pytest.raises(SystemExit) as excinfo:
        bzm_influx.main(["--help"])
    assert excinfo.value.code == 0


def test_cli_without_command_prints_help(capsys):
    assert bzm_influx.main([]) == 2
    assert "check" in capsys.readouterr().out


def test_cli_check_passes_with_full_env_and_never_prints_values(influx_env, capsys):
    assert bzm_influx.main(["check"]) == 0
    out = capsys.readouterr().out
    assert TOKEN not in out
    assert all(name in out for name in bzm_influx.ENV_VARS)


def test_cli_check_fails_naming_missing_vars(monkeypatch, influx_env, capsys):
    monkeypatch.delenv("INFLUX_TOKEN")
    monkeypatch.delenv("INFLUX_ORG")
    assert bzm_influx.main(["check"]) == 2
    captured = capsys.readouterr()
    assert "INFLUX_TOKEN: MISSING" in captured.out
    assert "INFLUX_TOKEN" in captured.err and "INFLUX_ORG" in captured.err


# --- live round-trip (auto-skips without a reachable InfluxDB 2.x) --------------------

_HAVE_INFLUX = bool(os.environ.get("INFLUX_URL") and os.environ.get("INFLUX_TOKEN"))


@pytest.mark.live
@pytest.mark.skipif(not _HAVE_INFLUX, reason="no INFLUX_URL/INFLUX_TOKEN in the environment")
def test_live_write_two_points_then_watermark_is_the_newer():
    """Round-trip against a real InfluxDB 2.x (e.g. a local `influxdb:2` container).

    Writes two points on a unique series (so reruns never collide), then asserts
    `query_watermark` scoped to that series returns the newer timestamp exactly.
    """
    writer = bzm_influx.InfluxWriter()
    run_tag = "livetest-%d" % time.time_ns()
    now = int(time.time())
    older, newer = now - 120, now - 60
    stats = writer.write(
        [
            "bzm_run,test_id=0,execution_id=%s avg_ms=1.0 %d" % (run_tag, older),
            "bzm_run,test_id=0,execution_id=%s avg_ms=2.0 %d" % (run_tag, newer),
        ]
    )
    assert stats == bzm_influx.WriteStats(
        attempted_lines=2, written_lines=2, failed_batches=0, retries=stats.retries
    )
    watermark = bzm_influx.query_watermark(
        measurement="bzm_run", tag_filters={"execution_id": run_tag}
    )
    assert watermark == newer
