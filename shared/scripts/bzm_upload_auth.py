#!/usr/bin/env python3
"""Upload a test's ``auth.json`` *asset* to a BlazeMeter test (REST fallback).

Some BlazeMeter tests carry an ``auth.json`` **test asset** — a file that
authenticates the *system under test* (the target the test drives), e.g. tokens or
login material the test script reads at runtime. This utility locates that asset
(in a downloaded-artifacts directory, or at an explicit ``--path``) and uploads it
to a test via the v4 *files* endpoint.

MCP-first (ADR-0004 / conventions §5): at the skill/conversation layer the
**MCP ``upload_assets`` action is PREFERRED** — it is higher-level, already
authenticated, and safer. A standalone script cannot call MCP tools (those are
model-side), so this module is the *documented REST fallback*. It posts to the one
documented endpoint below and is reached only when the MCP action is unavailable.

  REST v4 endpoint (the single place this URL is defined — see
  ``UPLOAD_PATH_TEMPLATE``):
      POST {base}/tests/{test_id}/files        (multipart/form-data, field "file")
  Base URL: https://a.blazemeter.com/api/v4 (see the v4 explorer). Authenticated
  with HTTP Basic auth using the Platform Credentials id:secret. A 201 response
  means the asset was stored. Re-uploading the same filename updates its contents.
  Reference: https://help.blazemeter.com/apidocs/performance/tests_upload_asset_files.htm

TWO DIFFERENT "auths" — kept rigorously distinct (CONTEXT.md):

  * **Platform Credentials** — the BlazeMeter API key ``{id, secret}`` that
    authenticates *this script to the BlazeMeter platform*. Resolved via the shared
    ``resolve_credentials`` resolver (ADR-0008); never hardcoded, never logged.
  * **The ``auth.json`` asset** — the file being uploaded. It authenticates the
    *system under test*, NOT BlazeMeter. This script treats it as an **opaque
    payload**: it is read as raw bytes and never parsed, and its contents are never
    interpreted as, compared to, or substituted for the Platform Credentials. The
    two never mix: the asset goes in the multipart *body*; the Platform Credentials
    go only in the ``Authorization`` header.

Safety contract:

  * Platform Credentials are never printed, logged, or embedded in output/errors;
    the ``Credentials`` object redacts itself and we only ever surface a masked id.
  * The ``auth.json`` asset bytes are uploaded but never echoed to stdout/stderr.

Standard library only (urllib, argparse, pathlib, json, uuid) so the CI ``--help``
smoke step and lint need nothing installed.

Usage:
    python bzm_upload_auth.py --test-id 1234567 --artifacts-dir ./downloaded
    python bzm_upload_auth.py --test-id 1234567 --path ./run/auth.json
    python bzm_upload_auth.py --help
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from resolve_credentials import CredentialError, Credentials, resolve_credentials

# --- REST v4 surface: defined in exactly one place ---------------------------

# Base of the BlazeMeter REST API v4 (see the v4 explorer). One constant so the
# endpoint lives in a single, auditable spot.
API_BASE = "https://a.blazemeter.com/api/v4"

# Upload-asset-files endpoint. ``{test_id}`` is substituted with the target test.
# multipart/form-data with a single part whose field name is "file".
UPLOAD_PATH_TEMPLATE = "/tests/{test_id}/files"

# The multipart field name the endpoint expects for the uploaded asset.
UPLOAD_FIELD_NAME = "file"

# The conventional asset filename this utility looks for inside an artifacts dir.
AUTH_ASSET_FILENAME = "auth.json"


class AuthAssetError(Exception):
    """Raised when the ``auth.json`` *asset* cannot be located or uploaded.

    Distinct from ``CredentialError`` (which is about the Platform Credentials).
    Messages reference file paths and the test id, never Platform Credential values
    and never the asset's contents.
    """


@dataclass(frozen=True)
class AuthAsset:
    """A located ``auth.json`` test asset, ready to upload.

    This is the *opaque* payload that authenticates the system under test. It is
    deliberately NOT the Platform Credentials: we hold its ``path`` and raw
    ``content`` bytes and never parse them as ``{id, secret}``.
    """

    path: Path
    filename: str
    content: bytes

    @property
    def size(self) -> int:
        return len(self.content)


def locate_auth_asset(
    *,
    explicit_path: str | None = None,
    artifacts_dir: str | None = None,
    filename: str = AUTH_ASSET_FILENAME,
) -> AuthAsset:
    """Locate the ``auth.json`` asset and read it as opaque bytes.

    Resolution order:
      1. ``explicit_path`` — use exactly this file if given.
      2. ``artifacts_dir`` — look for ``<dir>/<filename>`` (the asset as downloaded
         alongside a test run's artifacts).

    The file is read as raw bytes; its contents are **never parsed** — it is an
    opaque asset, not the Platform Credentials.

    Raises
    ------
    AuthAssetError
        If neither source is given, or the resolved path is missing / not a file.
        The message names the path or directory checked, never the asset contents.
    """
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if not path.is_file():
            raise AuthAssetError(
                "--path points to %r, which is not a readable file" % str(path)
            )
    elif artifacts_dir:
        directory = Path(artifacts_dir).expanduser()
        if not directory.is_dir():
            raise AuthAssetError(
                "--artifacts-dir %r is not a directory" % str(directory)
            )
        path = directory / filename
        if not path.is_file():
            raise AuthAssetError(
                "no %r asset found in artifacts dir %r" % (filename, str(directory))
            )
    else:
        raise AuthAssetError(
            "no auth asset location given: pass --path <file> or --artifacts-dir <dir>"
        )

    try:
        content = path.read_bytes()
    except OSError as exc:
        # Report the OS error, never the asset body.
        raise AuthAssetError(
            "could not read the auth asset at %r (%s)"
            % (str(path), exc.strerror or "unreadable")
        ) from None

    # Use the resolved file's own name so re-uploads target the same asset.
    return AuthAsset(path=path, filename=path.name, content=content)


def _encode_multipart(field_name: str, filename: str, content: bytes) -> tuple[bytes, str]:
    """Encode a single-file multipart/form-data body.

    Returns ``(body_bytes, content_type_header)``. Stdlib-only; no third-party
    multipart encoder. The asset ``content`` is embedded verbatim as opaque bytes.
    """
    # A boundary that cannot collide with the payload; uuid4 hex is body-safe.
    boundary = "----bzm-auth-asset-%s" % uuid.uuid4().hex
    dash = b"--"
    crlf = b"\r\n"
    boundary_bytes = boundary.encode("ascii")

    disposition = (
        'Content-Disposition: form-data; name="%s"; filename="%s"'
        % (field_name, filename)
    ).encode("utf-8")

    body = b"".join(
        [
            dash, boundary_bytes, crlf,
            disposition, crlf,
            b"Content-Type: application/octet-stream", crlf,
            crlf,
            content,
            crlf,
            dash, boundary_bytes, dash, crlf,
        ]
    )
    content_type = "multipart/form-data; boundary=%s" % boundary
    return body, content_type


def build_upload_request(
    test_id: str,
    asset: AuthAsset,
    credentials: Credentials,
    *,
    base_url: str = API_BASE,
) -> urllib.request.Request:
    """Build the authenticated multipart upload ``Request`` (no network I/O).

    The Platform Credentials authenticate the script to BlazeMeter via an HTTP Basic
    ``Authorization`` header; the ``auth.json`` asset rides in the multipart body.
    The two are placed in separate parts of the request and are never interchanged.

    Separated from the actual send so tests can assert URL/headers/body construction
    without a network — and verify the asset bytes go in the body while the Platform
    Credentials go only in the header.
    """
    test_id = str(test_id).strip()
    if not test_id:
        raise AuthAssetError("a non-empty --test-id is required to upload the asset")

    url = base_url.rstrip("/") + UPLOAD_PATH_TEMPLATE.format(test_id=test_id)
    body, content_type = _encode_multipart(UPLOAD_FIELD_NAME, asset.filename, asset.content)

    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("Authorization", _basic_auth_header(credentials))
    return request


def _basic_auth_header(credentials: Credentials) -> str:
    """Build the ``Basic <b64(id:secret)>`` header from the Platform Credentials.

    Reads id/secret only at the moment the header is built; the value is never
    returned to callers for logging and never placed anywhere but the header.
    """
    import base64

    creds = credentials.as_dict()
    token = "%s:%s" % (creds["id"], creds["secret"])
    encoded = base64.b64encode(token.encode("utf-8")).decode("ascii")
    return "Basic %s" % encoded


@dataclass(frozen=True)
class UploadResult:
    """Outcome of an upload attempt, safe to print (no secrets, no asset body)."""

    test_id: str
    filename: str
    status: int
    ok: bool


def upload_auth_asset(
    test_id: str,
    asset: AuthAsset,
    credentials: Credentials,
    *,
    base_url: str = API_BASE,
    opener: urllib.request.OpenerDirector | None = None,
    timeout: float = 60.0,
) -> UploadResult:
    """Send the built request and report the outcome.

    ``opener`` is injectable so tests can supply a fake transport and assert on the
    request without touching the network. A 2xx status is success (the endpoint
    returns 201 Created on a stored asset).
    """
    request = build_upload_request(test_id, asset, credentials, base_url=base_url)
    do_open = (opener.open if opener is not None else urllib.request.urlopen)
    try:
        response = do_open(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        # Surface the status; never echo the asset body or Platform Credentials.
        return UploadResult(
            test_id=str(test_id), filename=asset.filename, status=exc.code, ok=False
        )
    status = getattr(response, "status", None) or response.getcode()
    closer = getattr(response, "close", None)
    if callable(closer):
        closer()
    return UploadResult(
        test_id=str(test_id),
        filename=asset.filename,
        status=int(status),
        ok=200 <= int(status) < 300,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Upload a test's auth.json ASSET (which authenticates the system under "
            "test) to a BlazeMeter test via the REST v4 files endpoint. This is the "
            "documented REST fallback; prefer the MCP upload_assets action at the "
            "skill layer (ADR-0004). The asset is distinct from the BlazeMeter "
            "Platform Credentials, which are resolved from the MCP's env vars and "
            "used only to authenticate this script."
        ),
    )
    parser.add_argument(
        "--test-id",
        required=True,
        help="ID of the BlazeMeter test to upload the auth.json asset to.",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--path",
        help="Explicit path to the auth.json asset file to upload.",
    )
    source.add_argument(
        "--artifacts-dir",
        help="Directory of downloaded artifacts to find the auth.json asset in.",
    )
    parser.add_argument(
        "--filename",
        default=AUTH_ASSET_FILENAME,
        help="Asset filename to look for in --artifacts-dir (default: auth.json).",
    )
    return parser


def main(argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Locate the opaque asset first — no credentials or network needed for this.
    try:
        asset = locate_auth_asset(
            explicit_path=args.path,
            artifacts_dir=args.artifacts_dir,
            filename=args.filename,
        )
    except AuthAssetError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    # Resolve Platform Credentials separately (never confused with the asset).
    try:
        credentials = resolve_credentials(env)
    except CredentialError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1

    try:
        result = upload_auth_asset(args.test_id, asset, credentials)
    except urllib.error.URLError as exc:
        # Network/transport failure: report the reason, never the asset or creds.
        print("error: upload failed to reach BlazeMeter (%s)" % exc.reason, file=sys.stderr)
        return 1

    if result.ok:
        print(
            "Uploaded auth asset %r to test %s (HTTP %d)."
            % (result.filename, result.test_id, result.status)
        )
        return 0
    print(
        "error: upload of auth asset %r to test %s was rejected (HTTP %d)"
        % (result.filename, result.test_id, result.status),
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
