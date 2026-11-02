#!/usr/bin/env python3
"""InfluxDB 2.x client for the BlazeMeter export pipeline (write + watermark query).

`bzm_fetch.py export` turns BlazeMeter runs into InfluxDB line protocol; this module
is the Influx side of that pipeline: batched, gzipped writes to the v2 write API
(`POST /api/v2/write?org=&bucket=&precision=s`), and the Flux watermark query that
incremental sync uses ("newest exported point for this scope" -> export since then).

Write semantics: lines go up in batches of WRITE_BATCH_LINES, each batch gzipped.
429/5xx responses are retried with backoff (honoring `Retry-After`); a batch that
still fails after all attempts is recorded in the returned `WriteStats` and the
remaining batches continue, so a partial push is never silently presented as
complete — `attempted_lines`, `written_lines`, `failed_batches`, and `retries`
tell the truth. Re-pushing the same lines is safe: Influx upserts points with an
identical series key + timestamp.

Configuration is environment-only (never on argv):

  INFLUX_URL     base URL of the InfluxDB 2.x instance, e.g. http://localhost:8086
  INFLUX_TOKEN   API token — never on argv, never echoed, never in errors or repr
  INFLUX_ORG     organization name or id
  INFLUX_BUCKET  default destination bucket (overridable per call / per writer)

Exit codes (CLI): 0 success; 2 usage/configuration errors.

Standard-library only (urllib + gzip), like every script in this directory, so
users need nothing installed. All HTTP goes through one seam (`Transport.post`)
so tests inject canned responses and never touch the network. The `check` action
only inspects the environment — no network — so CI's --help/smoke loop stays
offline.

Usage:
    python bzm_influx.py --help
    python bzm_influx.py check
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone

USER_AGENT = "perforce-skills-bzm-influx/1"
ENV_VARS = ("INFLUX_URL", "INFLUX_TOKEN", "INFLUX_ORG", "INFLUX_BUCKET")

# The v2 write API caps neither lines nor body size hard, but ~5k lines per POST
# is the ballpark the Influx docs recommend for throughput; it also bounds memory
# (lines are streamed in, one batch materialized at a time).
WRITE_BATCH_LINES = 5000
MAX_ATTEMPTS = 3  # per batch: 1 try + up to 2 retries, same posture as bzm_fetch

WRITE_HEADERS = {
    "Content-Type": "text/plain; charset=utf-8",
    "Content-Encoding": "gzip",
}
QUERY_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/csv",
}


class InfluxQueryError(Exception):
    """The query API answered, but with a Flux error table instead of data."""


class InfluxConfigError(Exception):
    """Missing/incomplete environment configuration. Names the variable, never a value."""


@dataclass
class WriteStats:
    """Honest accounting for one `InfluxWriter.write` call."""

    attempted_lines: int = 0
    written_lines: int = 0
    failed_batches: int = 0
    retries: int = 0


# --- configuration (environment-only; the token never leaves the Transport) ----


def _require_env(name: str, env: dict[str, str] | None = None) -> str:
    """Read one required variable. The error names the variable, never any value."""
    env = os.environ if env is None else env
    value = env.get(name)
    if not value:
        raise InfluxConfigError(
            "%s is not set: the Influx destination is configured only via the "
            "environment (%s)" % (name, ", ".join(ENV_VARS))
        )
    return value


# --- transport (the single HTTP seam; tests replace this object) ---------------


class Transport:
    """All InfluxDB HTTP goes through `post` — one attempt per call.

    Retry policy deliberately lives in `_post_with_retry` (not here, unlike
    bzm_fetch's Transport) so `WriteStats.retries` can count every backoff.
    The token is stored only inside the prepared headers and never appears in
    `repr` or in any exception this class raises.
    """

    def __init__(self, url: str, token: str, timeout: float = 30.0):
        self._base_url = url.rstrip("/")
        self._headers = {"Authorization": "Token " + token, "User-Agent": USER_AGENT}
        self._timeout = timeout

    def __repr__(self) -> str:  # never the token
        return "Transport(url=%r)" % self._base_url

    def post(self, path: str, params: dict | None, body: bytes, headers: dict) -> bytes:
        url = self._base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(sorted(params.items()))
        req = urllib.request.Request(
            url, data=body, headers={**self._headers, **headers}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            return resp.read()


def _post_with_retry(
    transport,
    path: str,
    params: dict | None,
    body: bytes,
    headers: dict,
    *,
    stats: WriteStats | None = None,
    sleep=time.sleep,
    max_attempts: int = MAX_ATTEMPTS,
) -> bytes:
    """POST with backoff on 429/5xx (honoring Retry-After); other 4xx raise at once.

    Every backoff is counted on `stats.retries` (when given) — including the ones
    spent on a batch that ultimately fails — so the stats stay honest.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return transport.post(path, params, body, headers)
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code != 429 and exc.code < 500:
                raise  # 4xx other than 429 will not improve with retries
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
        except (urllib.error.URLError, TimeoutError) as exc:
            last_exc = exc
            retry_after = None
        if attempt < max_attempts:
            try:
                delay = float(retry_after) if retry_after else 2.0 ** (attempt - 1)
            except ValueError:
                delay = 2.0 ** (attempt - 1)
            if stats is not None:
                stats.retries += 1
            sleep(delay)
    raise last_exc  # type: ignore[misc]


# --- writer ---------------------------------------------------------------------


def _iter_batches(lines: Iterable[str], size: int) -> Iterator[list[str]]:
    batch: list[str] = []
    for line in lines:
        batch.append(line)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


class InfluxWriter:
    """Batched line-protocol writer against `POST /api/v2/write` (precision=s).

    `bucket` defaults to INFLUX_BUCKET; `points_bucket` is the optional second
    destination for high-volume point series (a shorter-retention bucket), and
    falls back to the main bucket when the caller keeps everything together.
    Callers route per write: `writer.write(run_lines)` /
    `writer.write(point_lines, bucket=writer.points_bucket)`.
    """

    def __init__(self, bucket: str | None = None, points_bucket: str | None = None):
        self._org = _require_env("INFLUX_ORG")
        self.bucket = bucket or _require_env("INFLUX_BUCKET")
        self.points_bucket = points_bucket or self.bucket
        self._transport = Transport(_require_env("INFLUX_URL"), _require_env("INFLUX_TOKEN"))
        self._sleep = time.sleep  # tests swap this to observe backoff without waiting

    def __repr__(self) -> str:  # never the token (it lives only in the Transport headers)
        return "InfluxWriter(org=%r, bucket=%r, points_bucket=%r)" % (
            self._org,
            self.bucket,
            self.points_bucket,
        )

    def write(self, lines: Iterable[str], *, bucket: str | None = None) -> WriteStats:
        """Push line-protocol lines in gzipped batches; never raises per-batch errors.

        A batch that exhausts its retries increments `failed_batches` and the walk
        continues with the next batch, so one bad window of lines cannot abort the
        whole export. Callers decide from the returned stats whether the push was
        complete.
        """
        params = {"org": self._org, "bucket": bucket or self.bucket, "precision": "s"}
        stats = WriteStats()
        for batch in _iter_batches(lines, WRITE_BATCH_LINES):
            stats.attempted_lines += len(batch)
            body = gzip.compress("\n".join(batch).encode("utf-8"))
            try:
                _post_with_retry(
                    self._transport,
                    "/api/v2/write",
                    params,
                    body,
                    WRITE_HEADERS,
                    stats=stats,
                    sleep=self._sleep,
                )
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                stats.failed_batches += 1
                continue
            stats.written_lines += len(batch)
        return stats


# --- watermark query (pure helpers first, fixture-tested without a transport) ---


def flux_string(value: str) -> str:
    """Quote a value as a Flux string literal (escaping backslash and quote)."""
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def build_watermark_flux(*, bucket: str, measurement: str, tag_filters: dict[str, str]) -> str:
    """The Flux query for "newest point matching these tag equalities".

    `keep` strips every column but `_time` FIRST — a measurement mixes float and
    string fields (names ride along as fields), and grouping tables of different
    value types together is a schema collision Influx rejects with HTTP 400.
    Only then can `group()` merge the matching series into one table, where
    `sort`+`last` yield exactly one `_time` row (or an empty result when nothing
    matches). Tags are emitted sorted so the query is deterministic and
    fixture-comparable.
    """
    parts = [
        "from(bucket: %s)" % flux_string(bucket),
        "|> range(start: 0)",
        "|> filter(fn: (r) => r._measurement == %s)" % flux_string(measurement),
    ]
    if tag_filters:
        equalities = " and ".join(
            "r[%s] == %s" % (flux_string(key), flux_string(value))
            for key, value in sorted(tag_filters.items())
        )
        parts.append("|> filter(fn: (r) => %s)" % equalities)
    parts += [
        '|> keep(columns: ["_time"])',
        "|> group()",
        '|> sort(columns: ["_time"])',
        '|> last(column: "_time")',
    ]
    return "\n  ".join(parts)


def _rfc3339_to_epoch(value: str) -> int:
    """Parse an annotated-CSV RFC3339 timestamp into epoch seconds (fraction dropped)."""
    value = re.sub(r"\.\d+", "", value.strip()).replace("Z", "+00:00")
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def watermark_from_csv(text: str) -> int | None:
    """Newest `_time` in an annotated-CSV query response, or None when it has no rows.

    Annotation lines start with `#`; each table re-states its header row (the one
    containing `_time`), and blank lines separate tables — so the parser tracks
    the current header's `_time` column and takes the max over every data row.
    """
    newest: int | None = None
    time_idx: int | None = None
    error_idx: int | None = None
    for row in csv.reader(io.StringIO(text)):
        if not row or row[0].startswith("#"):
            continue
        if "_time" in row or "error" in row:
            # A header row. Influx can answer HTTP 200 with an error table
            # (columns error,reference) mid-stream - that must surface as a
            # failure, never be misread as "no data" (sync would silently
            # fall back to the default window).
            time_idx = row.index("_time") if "_time" in row else None
            error_idx = row.index("error") if "error" in row and "reference" in row else None
            continue
        if error_idx is not None and error_idx < len(row) and row[error_idx]:
            raise InfluxQueryError(row[error_idx])
        if time_idx is None or time_idx >= len(row) or not row[time_idx]:
            continue
        ts = _rfc3339_to_epoch(row[time_idx])
        newest = ts if newest is None else max(newest, ts)
    return newest


def query_watermark(*, measurement: str, tag_filters: dict[str, str]) -> int | None:
    """Newest point timestamp (epoch s) matching tags, or None if none exist.

    Runs the Flux watermark query against `POST /api/v2/query` on the default
    bucket (INFLUX_BUCKET) — sync mode derives its "since" from this so no local
    state file is ever needed.
    """
    transport = Transport(_require_env("INFLUX_URL"), _require_env("INFLUX_TOKEN"))
    flux = build_watermark_flux(
        bucket=_require_env("INFLUX_BUCKET"), measurement=measurement, tag_filters=tag_filters
    )
    body = json.dumps({"query": flux, "type": "flux"}).encode("utf-8")
    text = _post_with_retry(
        transport, "/api/v2/query", {"org": _require_env("INFLUX_ORG")}, body, QUERY_HEADERS
    ).decode("utf-8")
    return watermark_from_csv(text)


# --- CLI (no-network `check` so the CI --help smoke covers this script) ---------


def cmd_check(args) -> int:
    """Report which INFLUX_* variables are set — names only, never values."""
    missing = [name for name in ENV_VARS if not os.environ.get(name)]
    for name in ENV_VARS:
        print("%s: %s" % (name, "MISSING" if name in missing else "set"))
    if missing:
        print(
            "error: set %s in the environment (values are never passed on argv)"
            % ", ".join(missing),
            file=sys.stderr,
        )
        return 2
    print("Influx environment configuration is complete (no network check performed).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="InfluxDB 2.x write/watermark client for BlazeMeter exports. "
        "Configured only via the environment: INFLUX_URL, INFLUX_TOKEN, INFLUX_ORG, "
        "INFLUX_BUCKET — never on argv. The write path is used as a module by "
        "`bzm_fetch.py export`.",
    )
    sub = parser.add_subparsers(dest="command")

    p_check = sub.add_parser(
        "check", help="Verify the INFLUX_* environment variables are set (no network)."
    )
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 2
    try:
        return args.func(args)
    except InfluxConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
