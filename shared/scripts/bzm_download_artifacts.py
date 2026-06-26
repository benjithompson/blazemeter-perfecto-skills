#!/usr/bin/env python3
"""Download a BlazeMeter execution's per-location ``artifacts.zip`` files in parallel.

Given a BlazeMeter **execution** (a *master*, a.k.a. report, id), this enumerates the
execution's per-location sessions, downloads each session's ``artifacts.zip`` concurrently,
extracts each into a per-location subdirectory of a user-chosen output dir, and reports which
expected artifacts were present or missing per location.

MCP-first, REST as the documented fallback (ADR-0004 / conventions §5)
----------------------------------------------------------------------
At the skill/conversation layer the **BlazeMeter MCP** is preferred for everything it covers.
But a standalone, dependency-free Python script cannot call MCP tools — those are model-side —
and the MCP does not expose a "download every location's artifacts.zip to disk" action. So this
script is the documented REST v4 fallback for that one capability. It talks to the BlazeMeter
REST API v4 (base ``https://a.blazemeter.com/api/v4``; reason from the v4 explorer at
https://a.blazemeter.com/api/v4/explorer/, not from memory). Two endpoints are used, each named
in exactly one place below (see ``SESSIONS_PATH`` and ``SESSION_LOGS_PATH``):

  * ``GET /masters/{masterId}/sessions`` — the execution's sessions, one per engine/location.
  * ``GET /sessions/{sessionId}/reports/logs`` — that session's downloadable log artifacts,
    among which is the ``artifacts.zip`` we want (carried as a ``dataUrl``).

Credentials (conventions §6, ADR-0008)
--------------------------------------
Authentication is **purely** via the shared resolver (``resolve_credentials``), which reuses the
BlazeMeter MCP's own env vars — no second setup, no hardcoded key path. BlazeMeter REST v4 uses
HTTP Basic auth with ``id:secret``. Credentials are **never** printed, logged, or written into
any artifact, error, or report: the ``Authorization`` header is built once and never echoed.

A test asset's ``auth.json`` (used to authenticate the *system under test*) is a different thing
from these Platform Credentials — this script only ever uses Platform Credentials, and only to
talk to BlazeMeter itself.

Safety
------
* The output dir is **always user-supplied** (``--out``); there is no personal/hardcoded default.
* Zip extraction is guarded against path traversal (``..`` / absolute members / symlinks): a
  malicious or buggy archive cannot write outside its per-location directory.

This module is standard-library only (``urllib``, ``zipfile``, ``json``,
``concurrent.futures``, ``argparse``, ``pathlib``) so CI's lint and ``--help`` smoke steps need
nothing installed. ``--help`` exits 0 with no credentials and no network.

Usage:
    python bzm_download_artifacts.py --master 12345678 --out ./artifacts
    python bzm_download_artifacts.py --master 12345678 --out ./out --expect artifacts.zip kpi.jtl
    python bzm_download_artifacts.py --help
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Sequence
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from resolve_credentials import Credentials, CredentialError, resolve_credentials

# --- REST v4 surface (each endpoint named in exactly ONE place) --------------

API_BASE = "https://a.blazemeter.com/api/v4"
#: Sessions for an execution/master. Formatted with the master id.
SESSIONS_PATH = "/masters/{master_id}/sessions"
#: Downloadable log artifacts for a single session. Formatted with the session id.
SESSION_LOGS_PATH = "/sessions/{session_id}/reports/logs"

#: The artifact every load-test session is expected to produce; the report defaults to it.
ARTIFACTS_ZIP = "artifacts.zip"

DEFAULT_TIMEOUT = 120  # seconds, per HTTP request
DEFAULT_WORKERS = 8


class DownloadError(Exception):
    """A user-facing failure that never carries a credential or response body."""


# --- value types -------------------------------------------------------------


@dataclass(frozen=True)
class Session:
    """One per-location session within an execution.

    ``location`` is the engine location label (e.g. ``us-east-1``); ``name`` is the session's
    display name. Either may be empty in the API response, so we fall back to the id for naming.
    """

    id: str
    location: str
    name: str

    def label(self) -> str:
        """A stable, filesystem-safe folder name for this session's artifacts."""
        raw = self.location or self.name or self.id
        return _safe_component(raw)


@dataclass
class LocationResult:
    """The per-location outcome reported back to the caller."""

    session_id: str
    location: str
    out_dir: str | None = None
    present: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.missing


# --- HTTP (mockable seam) ----------------------------------------------------


def _basic_auth_header(creds: Credentials) -> str:
    """Build the HTTP Basic ``Authorization`` header value for ``id:secret``.

    Returned for use in request headers only; the caller never prints it.
    """
    token = base64.b64encode(("%s:%s" % (creds.id, creds.secret)).encode("utf-8")).decode("ascii")
    return "Basic " + token


def build_request(url: str, creds: Credentials) -> urllib.request.Request:
    """Construct an authenticated GET ``Request`` for ``url``.

    Auth is HTTP Basic from the resolved Platform Credentials. The header is set on the request
    object but is never logged.
    """
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", _basic_auth_header(creds))
    req.add_header("Accept", "application/json")
    return req


def _http_get_json(url: str, creds: Credentials, *, timeout: int, opener) -> dict:
    """GET ``url`` and parse a JSON object response.

    ``opener`` is injected (defaults wire to ``urllib.request.urlopen``) so tests mock the
    network without monkeypatching globals. Errors never include the credential or raw body.
    """
    req = build_request(url, creds)
    try:
        with opener(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise DownloadError("GET %s failed: HTTP %s" % (_redact_url(url), exc.code)) from None
    except urllib.error.URLError as exc:
        raise DownloadError("GET %s failed: %s" % (_redact_url(url), exc.reason)) from None

    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise DownloadError("GET %s did not return valid JSON" % _redact_url(url)) from None
    if not isinstance(data, dict):
        raise DownloadError("GET %s returned an unexpected JSON shape" % _redact_url(url))
    return data


def _http_get_bytes(url: str, creds: Credentials, *, timeout: int, opener) -> bytes:
    """GET ``url`` and return the raw bytes (used for the artifacts.zip download)."""
    req = build_request(url, creds)
    try:
        with opener(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        raise DownloadError("download %s failed: HTTP %s" % (_redact_url(url), exc.code)) from None
    except urllib.error.URLError as exc:
        raise DownloadError("download %s failed: %s" % (_redact_url(url), exc.reason)) from None


# --- parsing -----------------------------------------------------------------


def parse_sessions(payload: Mapping) -> list[Session]:
    """Parse ``GET /masters/{id}/sessions`` into ``Session`` objects.

    The v4 response wraps the list as ``{"result": {"sessions": [...]}}``; some deployments
    return ``{"result": [...]}`` directly. Both are accepted. Entries without an ``id`` are
    skipped rather than guessed.
    """
    result = payload.get("result")
    if isinstance(result, Mapping):
        raw_sessions = result.get("sessions", [])
    elif isinstance(result, list):
        raw_sessions = result
    else:
        raw_sessions = []

    sessions: list[Session] = []
    for entry in raw_sessions:
        if not isinstance(entry, Mapping):
            continue
        sid = entry.get("id")
        if not sid:
            continue
        location = entry.get("locationName") or entry.get("location") or ""
        name = entry.get("name") or ""
        sessions.append(Session(id=str(sid), location=str(location), name=str(name)))
    return sessions


def find_artifacts_url(payload: Mapping, *, filename: str = ARTIFACTS_ZIP) -> str | None:
    """Find the download URL for ``filename`` in a session's reports/logs response.

    The v4 response is ``{"result": {"data": [{"filename": ..., "dataUrl": ...}, ...]}}``.
    Returns the matching ``dataUrl`` (or ``url``) or ``None`` if that artifact isn't present.
    """
    result = payload.get("result")
    if isinstance(result, Mapping):
        rows = result.get("data", [])
    elif isinstance(result, list):
        rows = result
    else:
        rows = []

    for row in rows:
        if not isinstance(row, Mapping):
            continue
        if row.get("filename") == filename:
            url = row.get("dataUrl") or row.get("url")
            return str(url) if url else None
    return None


# --- safe zip extraction -----------------------------------------------------


def _is_within(base: Path, target: Path) -> bool:
    """True iff ``target`` is ``base`` itself or lives beneath it (resolved)."""
    base_r = base.resolve()
    target_r = target.resolve()
    return target_r == base_r or base_r in target_r.parents


def safe_extract(zip_path_or_file, dest: Path) -> list[str]:
    """Extract a zip into ``dest``, refusing any member that escapes ``dest``.

    Guards against path traversal: absolute member paths, ``..`` components, and symlink members
    are rejected with a ``DownloadError`` rather than written. Returns the list of extracted
    member names (top-level-relative) on success.

    ``zip_path_or_file`` may be a path or an open binary file object.
    """
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[str] = []
    try:
        with ZipFile(zip_path_or_file) as zf:
            for member in zf.infolist():
                name = member.filename
                # Reject absolute paths and any traversal up and out of dest.
                pure = PurePosixPath(name)
                if pure.is_absolute() or name.startswith("/") or ".." in pure.parts:
                    raise DownloadError("refusing unsafe zip member path %r" % name)
                if _is_symlink_member(member):
                    raise DownloadError("refusing symlink zip member %r" % name)
                target = dest / Path(*pure.parts)
                if not _is_within(dest, target):
                    raise DownloadError("refusing zip member escaping output dir: %r" % name)
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(target, "wb") as out:
                    out.write(src.read())
                extracted.append(name)
    except BadZipFile:
        raise DownloadError("downloaded file is not a valid zip archive") from None
    return extracted


def _is_symlink_member(member) -> bool:
    """True if a ZipInfo member encodes a (unix) symlink in its external attrs."""
    # Upper 16 bits of external_attr hold the unix mode; 0xA000 == S_IFLNK.
    mode = (getattr(member, "external_attr", 0) >> 16) & 0xFFFF
    return (mode & 0xF000) == 0xA000


# --- reporting ---------------------------------------------------------------


def build_location_report(
    session: Session, extracted: Iterable[str], expected: Sequence[str]
) -> tuple[list[str], list[str]]:
    """Split ``expected`` artifacts into (present, missing) for one location.

    A member matches an expected artifact if its basename equals the expected name, so nested
    members (e.g. ``some/dir/kpi.jtl``) still count. Returns parallel sorted lists.
    """
    basenames = {PurePosixPath(name).name for name in extracted}
    present = sorted(name for name in expected if name in basenames)
    missing = sorted(name for name in expected if name not in basenames)
    return present, missing


# --- orchestration -----------------------------------------------------------


def _process_session(
    session: Session,
    creds: Credentials,
    out_root: Path,
    expected: Sequence[str],
    *,
    timeout: int,
    opener,
) -> LocationResult:
    """Download + extract + report for a single location. Never raises; captures errors."""
    res = LocationResult(session_id=session.id, location=session.location)
    try:
        logs = _http_get_json(
            API_BASE + SESSION_LOGS_PATH.format(session_id=session.id),
            creds,
            timeout=timeout,
            opener=opener,
        )
        url = find_artifacts_url(logs, filename=ARTIFACTS_ZIP)
        if url is None:
            res.error = "no %s available for this session" % ARTIFACTS_ZIP
            res.missing = sorted(expected)
            return res

        blob = _http_get_bytes(url, creds, timeout=timeout, opener=opener)
        dest = out_root / session.label()
        # Write the raw zip alongside its extraction for traceability, then extract safely.
        dest.mkdir(parents=True, exist_ok=True)
        zip_path = dest / ARTIFACTS_ZIP
        zip_path.write_bytes(blob)
        extracted = safe_extract(zip_path, dest)
        res.out_dir = str(dest)
        res.present, res.missing = build_location_report(session, extracted, expected)
    except DownloadError as exc:
        res.error = str(exc)
    return res


def download_execution_artifacts(
    master_id: str,
    out_root: Path,
    *,
    creds: Credentials,
    expected: Sequence[str] = (ARTIFACTS_ZIP,),
    workers: int = DEFAULT_WORKERS,
    timeout: int = DEFAULT_TIMEOUT,
    opener=None,
) -> list[LocationResult]:
    """Download and extract every location's artifacts for one execution, in parallel.

    Returns a ``LocationResult`` per session (location). ``opener`` is injected for tests;
    it defaults to ``urllib.request.urlopen``. Credentials never appear in any result.
    """
    if opener is None:
        opener = urllib.request.urlopen

    sessions_payload = _http_get_json(
        API_BASE + SESSIONS_PATH.format(master_id=master_id),
        creds,
        timeout=timeout,
        opener=opener,
    )
    sessions = parse_sessions(sessions_payload)
    if not sessions:
        return []

    out_root.mkdir(parents=True, exist_ok=True)
    results: list[LocationResult] = []
    max_workers = max(1, min(workers, len(sessions)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [
            pool.submit(
                _process_session,
                s,
                creds,
                out_root,
                expected,
                timeout=timeout,
                opener=opener,
            )
            for s in sessions
        ]
        for fut in concurrent.futures.as_completed(futures):
            results.append(fut.result())

    # Deterministic order for stable reporting regardless of completion order.
    results.sort(key=lambda r: (r.location, r.session_id))
    return results


# --- rendering / CLI ---------------------------------------------------------


def _redact_url(url: str) -> str:
    """Drop any query string (which can carry signed tokens) from a URL for display."""
    parts = urlsplit(url)
    base = "%s://%s%s" % (parts.scheme, parts.netloc, parts.path) if parts.scheme else parts.path
    return base or url


def _safe_component(raw: str) -> str:
    """Turn an arbitrary label into a single safe path component (no separators, no traversal)."""
    cleaned = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in raw.strip())
    # Collapse any run of dots so a label can never contain a ".." traversal segment.
    while ".." in cleaned:
        cleaned = cleaned.replace("..", ".")
    cleaned = cleaned.strip(".") or "session"
    return cleaned


def render_report(master_id: str, results: Sequence[LocationResult]) -> str:
    """Render a human-readable, credential-free summary of the run."""
    lines = ["Execution %s — %d location(s)" % (master_id, len(results))]
    if not results:
        lines.append("  (no sessions found for this execution)")
        return "\n".join(lines)
    for r in results:
        loc = r.location or r.session_id
        if r.error:
            lines.append("  [FAIL] %s: %s" % (loc, r.error))
            continue
        status = "ok" if r.ok else "incomplete"
        lines.append("  [%s] %s -> %s" % (status, loc, r.out_dir))
        if r.present:
            lines.append("    present: %s" % ", ".join(r.present))
        if r.missing:
            lines.append("    missing: %s" % ", ".join(r.missing))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Download and extract a BlazeMeter execution's per-location artifacts.zip "
        "files (REST v4 fallback; authenticated via the shared credential resolver).",
    )
    parser.add_argument(
        "--master",
        "--execution",
        dest="master",
        metavar="MASTER_ID",
        help="BlazeMeter execution (master / report) id to download artifacts for.",
    )
    parser.add_argument(
        "--out",
        metavar="DIR",
        help="Output directory to extract per-location artifacts into (required to run; "
        "no default — never a personal/hardcoded path).",
    )
    parser.add_argument(
        "--expect",
        nargs="+",
        default=[ARTIFACTS_ZIP],
        metavar="NAME",
        help="Expected artifact filenames to report presence/absence of per location "
        "(default: %s)." % ARTIFACTS_ZIP,
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help="Max parallel downloads (default: %d)." % DEFAULT_WORKERS,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Per-request timeout in seconds (default: %d)." % DEFAULT_TIMEOUT,
    )
    args = parser.parse_args(argv)

    # --help (handled by argparse above) must never need these; only a real run does.
    if not args.master or not args.out:
        parser.error("--master and --out are both required to run")

    try:
        creds = resolve_credentials()
    except CredentialError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    try:
        results = download_execution_artifacts(
            str(args.master),
            Path(args.out),
            creds=creds,
            expected=args.expect,
            workers=args.workers,
            timeout=args.timeout,
        )
    except DownloadError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    print(render_report(str(args.master), results))
    # Non-zero exit if any location failed or was incomplete, so callers can gate on it.
    return 0 if all(r.ok for r in results) and results else 2


if __name__ == "__main__":
    raise SystemExit(main())
