"""Fixture-driven tests for the BlazeMeter artifact-download utility.

The download flow has several pieces of deterministic logic worth testing without ever hitting
the network: building the authenticated (HTTP Basic) request from resolver-provided credentials,
parsing the sessions/locations list, locating the ``artifacts.zip`` URL, the per-location
presence/absence report, and — importantly — *safe* zip extraction that refuses path traversal.

The network is fully mocked: an injected ``opener`` stands in for ``urllib.request.urlopen``, so
no test touches BlazeMeter or the real filesystem outside pytest's ``tmp_path``. As with the
credential-resolver tests, we also assert that secrets never leak into requests we can see or
into error text.
"""

import base64
import io
import json
import zipfile

import pytest

import bzm_download_artifacts as dl
from resolve_credentials import Credentials, resolve_credentials

# Obviously-fake, low-entropy placeholders — NOT real credentials (kept un-secret-like so secret
# scanners don't flag the fixtures). The tests only need distinctive strings to assert on.
KEY_ID = "example-not-a-real-key-id"
SECRET = "example-not-a-real-secret-value"

CREDS = Credentials(id=KEY_ID, secret=SECRET, source="env")


# --- a tiny mock for the urlopen seam ----------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeOpener:
    """Records each Request and returns a queued response keyed by URL substring.

    Routes are matched by 'first substring found in the URL', so tests can map the sessions
    endpoint, the per-session logs endpoint, and the artifact dataUrl independently.
    """

    def __init__(self, routes):
        self._routes = routes
        self.requests = []

    def __call__(self, req, timeout=None):
        self.requests.append(req)
        url = req.full_url
        for needle, body in self._routes.items():
            if needle in url:
                payload = body() if callable(body) else body
                return _FakeResponse(payload)
        raise AssertionError("no fake route matched URL: %s" % url)


def _json_bytes(obj) -> bytes:
    return json.dumps(obj).encode("utf-8")


def _make_zip(members: dict) -> bytes:
    """Build an in-memory zip from {arcname: text} (or raw bytes)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in members.items():
            if isinstance(content, bytes):
                zf.writestr(name, content)
            else:
                zf.writestr(name, content)
    return buf.getvalue()


# --- credential reuse via the shared resolver (injected env) -----------------


def test_credentials_come_from_shared_resolver_env_pair():
    creds = resolve_credentials({"API_KEY_ID": KEY_ID, "API_KEY_SECRET": SECRET})
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}
    # The download utility consumes exactly this object's id/secret for Basic auth.
    header = dl._basic_auth_header(creds)
    decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
    assert decoded == "%s:%s" % (KEY_ID, SECRET)


# --- building the authenticated request --------------------------------------


def test_build_request_sets_basic_auth_header():
    req = dl.build_request(dl.API_BASE + "/masters/777/sessions", CREDS)
    assert req.get_method() == "GET"
    auth = req.get_header("Authorization")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    assert decoded == "%s:%s" % (KEY_ID, SECRET)


def test_build_request_uses_v4_base_in_one_place():
    # The endpoint constants are the single source of truth for the URL shape.
    assert dl.API_BASE == "https://a.blazemeter.com/api/v4"
    assert dl.SESSIONS_PATH.format(master_id="9") == "/masters/9/sessions"
    assert dl.SESSION_LOGS_PATH.format(session_id="s1") == "/sessions/s1/reports/logs"


# --- parsing the sessions / locations list -----------------------------------


def test_parse_sessions_nested_result_shape():
    payload = {
        "result": {
            "sessions": [
                {"id": "s-1", "locationName": "us-east-1", "name": "load"},
                {"id": "s-2", "location": "eu-west-1"},
            ]
        }
    }
    sessions = dl.parse_sessions(payload)
    assert [s.id for s in sessions] == ["s-1", "s-2"]
    assert sessions[0].location == "us-east-1"
    assert sessions[1].location == "eu-west-1"


def test_parse_sessions_flat_list_result_shape():
    payload = {"result": [{"id": "s-9", "locationName": "ap-south-1"}]}
    sessions = dl.parse_sessions(payload)
    assert len(sessions) == 1 and sessions[0].location == "ap-south-1"


def test_parse_sessions_skips_entries_without_id():
    payload = {"result": {"sessions": [{"locationName": "no-id"}, {"id": "ok"}]}}
    sessions = dl.parse_sessions(payload)
    assert [s.id for s in sessions] == ["ok"]


def test_parse_sessions_handles_empty_and_garbage():
    assert dl.parse_sessions({}) == []
    assert dl.parse_sessions({"result": None}) == []
    assert dl.parse_sessions({"result": {"sessions": ["nope", 5]}}) == []


def test_session_label_is_filesystem_safe():
    s = dl.Session(id="s1", location="us east/../1", name="")
    label = s.label()
    assert "/" not in label and ".." not in label


# --- locating the artifacts.zip URL ------------------------------------------


def test_find_artifacts_url_in_logs_payload():
    payload = {
        "result": {
            "data": [
                {"filename": "kpi.jtl", "dataUrl": "https://x/kpi"},
                {"filename": "artifacts.zip", "dataUrl": "https://x/artifacts"},
            ]
        }
    }
    assert dl.find_artifacts_url(payload) == "https://x/artifacts"


def test_find_artifacts_url_absent_returns_none():
    payload = {"result": {"data": [{"filename": "kpi.jtl", "dataUrl": "https://x/kpi"}]}}
    assert dl.find_artifacts_url(payload) is None


# --- per-location presence/absence report ------------------------------------


def test_build_location_report_present_and_missing():
    session = dl.Session(id="s1", location="us-east-1", name="")
    extracted = ["artifacts/kpi.jtl", "artifacts/error.jtl"]
    present, missing = dl.build_location_report(session, extracted, ["kpi.jtl", "simulation.log"])
    assert present == ["kpi.jtl"]
    assert missing == ["simulation.log"]


def test_build_location_report_matches_by_basename():
    session = dl.Session(id="s1", location="loc", name="")
    present, missing = dl.build_location_report(session, ["deep/nested/dir/run.log"], ["run.log"])
    assert present == ["run.log"] and missing == []


# --- safe zip extraction (path-traversal guard) ------------------------------


def test_safe_extract_writes_members(tmp_path):
    blob = _make_zip({"kpi.jtl": "rows", "sub/error.jtl": "errs"})
    dest = tmp_path / "loc"
    extracted = dl.safe_extract(io.BytesIO(blob), dest)
    assert (dest / "kpi.jtl").read_text() == "rows"
    assert (dest / "sub" / "error.jtl").read_text() == "errs"
    assert set(extracted) == {"kpi.jtl", "sub/error.jtl"}


def test_safe_extract_rejects_parent_traversal(tmp_path):
    blob = _make_zip({"../escape.txt": "pwned"})
    dest = tmp_path / "loc"
    with pytest.raises(dl.DownloadError):
        dl.safe_extract(io.BytesIO(blob), dest)
    # Nothing escaped to the parent dir.
    assert not (tmp_path / "escape.txt").exists()


def test_safe_extract_rejects_absolute_member(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("/abs/evil.txt", "x")
    dest = tmp_path / "loc"
    with pytest.raises(dl.DownloadError):
        dl.safe_extract(io.BytesIO(buf.getvalue()), dest)


def test_safe_extract_rejects_symlink_member(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("link")
        # S_IFLNK (0xA000) | 0777 in the high 16 bits marks a unix symlink.
        info.external_attr = (0xA000 | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    dest = tmp_path / "loc"
    with pytest.raises(dl.DownloadError):
        dl.safe_extract(io.BytesIO(buf.getvalue()), dest)


def test_safe_extract_rejects_non_zip(tmp_path):
    with pytest.raises(dl.DownloadError):
        dl.safe_extract(io.BytesIO(b"not a zip at all"), tmp_path / "loc")


# --- end-to-end orchestration with a mocked network --------------------------


def _two_location_routes():
    sessions = {
        "result": {
            "sessions": [
                {"id": "s-1", "locationName": "us-east-1"},
                {"id": "s-2", "locationName": "eu-west-1"},
            ]
        }
    }
    logs_1 = {"result": {"data": [{"filename": "artifacts.zip", "dataUrl": "https://dl/east"}]}}
    logs_2 = {"result": {"data": [{"filename": "artifacts.zip", "dataUrl": "https://dl/west"}]}}
    zip_east = _make_zip({"kpi.jtl": "east", "simulation.log": "log"})
    zip_west = _make_zip({"kpi.jtl": "west"})  # missing simulation.log
    return {
        "/masters/123/sessions": _json_bytes(sessions),
        "/sessions/s-1/reports/logs": _json_bytes(logs_1),
        "/sessions/s-2/reports/logs": _json_bytes(logs_2),
        "https://dl/east": zip_east,
        "https://dl/west": zip_west,
    }


def test_download_execution_extracts_and_reports(tmp_path):
    opener = FakeOpener(_two_location_routes())
    results = dl.download_execution_artifacts(
        "123",
        tmp_path,
        creds=CREDS,
        expected=["kpi.jtl", "simulation.log"],
        opener=opener,
    )
    by_loc = {r.location: r for r in results}
    assert set(by_loc) == {"us-east-1", "eu-west-1"}

    east = by_loc["us-east-1"]
    assert east.ok
    assert east.present == ["kpi.jtl", "simulation.log"]
    assert (tmp_path / "us-east-1" / "kpi.jtl").read_text() == "east"

    west = by_loc["eu-west-1"]
    assert not west.ok
    assert west.missing == ["simulation.log"]
    assert west.present == ["kpi.jtl"]


def test_download_reports_missing_artifacts_zip(tmp_path):
    routes = {
        "/masters/55/sessions": _json_bytes(
            {"result": {"sessions": [{"id": "s-x", "locationName": "lone"}]}}
        ),
        "/sessions/s-x/reports/logs": _json_bytes(
            {"result": {"data": [{"filename": "kpi.jtl", "dataUrl": "https://dl/k"}]}}
        ),
    }
    opener = FakeOpener(routes)
    results = dl.download_execution_artifacts("55", tmp_path, creds=CREDS, opener=opener)
    assert len(results) == 1
    assert results[0].error and "artifacts.zip" in results[0].error


def test_download_no_sessions_returns_empty(tmp_path):
    routes = {"/masters/0/sessions": _json_bytes({"result": {"sessions": []}})}
    opener = FakeOpener(routes)
    results = dl.download_execution_artifacts("0", tmp_path, creds=CREDS, opener=opener)
    assert results == []


def test_download_requests_carry_basic_auth(tmp_path):
    opener = FakeOpener(_two_location_routes())
    dl.download_execution_artifacts("123", tmp_path, creds=CREDS, expected=["kpi.jtl"], opener=opener)
    # Every request the utility made must carry the Basic auth header.
    assert opener.requests
    for req in opener.requests:
        auth = req.get_header("Authorization")
        assert auth and auth.startswith("Basic ")


# --- the safety contract: secrets never leak ---------------------------------


def test_results_and_report_never_leak_credentials(tmp_path):
    opener = FakeOpener(_two_location_routes())
    results = dl.download_execution_artifacts(
        "123", tmp_path, creds=CREDS, expected=["kpi.jtl"], opener=opener
    )
    report = dl.render_report("123", results)
    blob = report + repr(results)
    assert SECRET not in blob
    assert KEY_ID not in blob


def test_http_error_message_never_leaks_credentials():
    import urllib.error

    def boom(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", hdrs=None, fp=None)

    with pytest.raises(dl.DownloadError) as exc:
        dl._http_get_json(dl.API_BASE + "/masters/1/sessions", CREDS, timeout=5, opener=boom)
    msg = str(exc.value)
    assert SECRET not in msg and KEY_ID not in msg
    assert "403" in msg


def test_redact_url_drops_query_tokens():
    redacted = dl._redact_url("https://dl/east?signature=example-token&x=1")
    assert "signature" not in redacted
    assert "example-token" not in redacted


# --- CLI surface (the --help smoke target) -----------------------------------


def test_cli_help_exits_zero_without_credentials(capsys):
    with pytest.raises(SystemExit) as exc:
        dl.main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--master" in out and "--out" in out


def test_cli_requires_master_and_out(capsys):
    # Missing required run args must error out (argparse exits non-zero) before any network.
    with pytest.raises(SystemExit) as exc:
        dl.main([])
    assert exc.value.code != 0
