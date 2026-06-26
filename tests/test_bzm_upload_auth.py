"""Fixture-driven tests for the auth.json asset upload utility.

The deterministic, security-sensitive parts of ``bzm_upload_auth`` get real tests
over their *external behaviour*: locating the asset, building the authenticated
multipart request, reusing the Platform Credentials via an injected env — and, above
all, keeping the ``auth.json`` *asset* (which authenticates the system under test)
rigorously distinct from the BlazeMeter *Platform Credentials*. The HTTP layer is
mocked so no network is required.

Two strings stand in for the two different secrets, so every test can assert which
one ends up where:

  * ``PLATFORM_SECRET`` — the Platform Credential secret. Belongs ONLY in the
    Authorization header, and must never appear in stdout/stderr/errors.
  * the asset body bytes — the opaque ``auth.json`` payload. Belongs ONLY in the
    multipart body, and must never be parsed as ``{id, secret}`` nor printed.

All placeholders are obviously-fake, low-entropy strings so secret scanners don't
flag the fixtures.
"""

import base64
import json
import urllib.error

import pytest

import bzm_upload_auth as ua
import resolve_credentials as rc

# Obviously-fake, low-entropy placeholders — NOT real secrets.
PLATFORM_ID = "example-platform-id-123"
PLATFORM_SECRET = "example-not-a-real-platform-secret"
TEST_ID = "1234567"

# The asset's *contents* — an opaque payload. Deliberately shaped like JSON with a
# DIFFERENT field name than the Platform Credentials so a test can prove the script
# never reads it as {id, secret}.
ASSET_BODY = json.dumps({"sut_token": "example-system-under-test-token"}).encode("utf-8")


def _platform_credentials():
    return rc.Credentials(id=PLATFORM_ID, secret=PLATFORM_SECRET, source="env")


def _write_asset(tmp_path, body=ASSET_BODY, name="auth.json"):
    path = tmp_path / name
    path.write_bytes(body)
    return path


class _FakeResponse:
    def __init__(self, status=201):
        self.status = status

    def getcode(self):
        return self.status

    def close(self):
        pass


class _RecordingOpener:
    """Captures the Request passed to ``open`` and returns a canned response."""

    def __init__(self, response=None, error=None):
        self._response = response or _FakeResponse(201)
        self._error = error
        self.request = None
        self.timeout = None

    def open(self, request, timeout=None):
        self.request = request
        self.timeout = timeout
        if self._error is not None:
            raise self._error
        return self._response


# --- locating the auth.json asset --------------------------------------------


def test_locate_in_artifacts_dir(tmp_path):
    _write_asset(tmp_path)
    asset = ua.locate_auth_asset(artifacts_dir=str(tmp_path))
    assert asset.filename == "auth.json"
    assert asset.content == ASSET_BODY


def test_locate_explicit_path(tmp_path):
    path = _write_asset(tmp_path, name="run-auth.json")
    asset = ua.locate_auth_asset(explicit_path=str(path))
    assert asset.path == path
    assert asset.filename == "run-auth.json"
    assert asset.content == ASSET_BODY


def test_locate_missing_in_dir_is_clear_error(tmp_path):
    with pytest.raises(ua.AuthAssetError) as exc:
        ua.locate_auth_asset(artifacts_dir=str(tmp_path))
    assert "auth.json" in str(exc.value)
    assert str(tmp_path) in str(exc.value)


def test_locate_explicit_path_missing_is_clear_error(tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(ua.AuthAssetError) as exc:
        ua.locate_auth_asset(explicit_path=str(missing))
    assert "not a readable file" in str(exc.value)


def test_locate_artifacts_dir_not_a_directory(tmp_path):
    not_a_dir = _write_asset(tmp_path)
    with pytest.raises(ua.AuthAssetError) as exc:
        ua.locate_auth_asset(artifacts_dir=str(not_a_dir))
    assert "not a directory" in str(exc.value)


def test_locate_requires_a_source():
    with pytest.raises(ua.AuthAssetError) as exc:
        ua.locate_auth_asset()
    assert "--path" in str(exc.value) and "--artifacts-dir" in str(exc.value)


def test_explicit_path_takes_precedence_over_dir(tmp_path):
    # Both given: the explicit file wins, the dir's auth.json is not consulted.
    _write_asset(tmp_path, body=b"dir-version", name="auth.json")
    explicit = _write_asset(tmp_path, body=b"explicit-version", name="explicit.json")
    asset = ua.locate_auth_asset(
        explicit_path=str(explicit), artifacts_dir=str(tmp_path)
    )
    assert asset.content == b"explicit-version"


# --- the asset is opaque, never parsed as Platform Credentials ---------------


def test_asset_is_read_as_opaque_bytes_not_parsed(tmp_path):
    # An asset that is not even valid JSON must load fine — it's opaque bytes.
    raw = b"\x00\x01 not json at all \xff"
    path = _write_asset(tmp_path, body=raw, name="auth.json")
    asset = ua.locate_auth_asset(explicit_path=str(path))
    assert asset.content == raw
    assert asset.size == len(raw)


def test_asset_contents_never_become_platform_credentials(tmp_path):
    # Even if the asset happens to contain id/secret keys, the locator must NOT turn
    # it into Platform Credentials — those come only from the resolver.
    sneaky = json.dumps({"id": "asset-id", "secret": "asset-secret"}).encode("utf-8")
    path = _write_asset(tmp_path, body=sneaky, name="auth.json")
    asset = ua.locate_auth_asset(explicit_path=str(path))
    # It is an AuthAsset, not Credentials, and exposes no id/secret accessor.
    assert isinstance(asset, ua.AuthAsset)
    assert not hasattr(asset, "as_dict")
    assert asset.content == sneaky


# --- building the authenticated multipart upload request ---------------------


def test_build_request_url_targets_the_test_files_endpoint(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    assert req.full_url == "https://a.blazemeter.com/api/v4/tests/%s/files" % TEST_ID
    assert req.method == "POST"


def test_build_request_is_multipart_with_file_field(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    content_type = req.get_header("Content-type")
    assert content_type.startswith("multipart/form-data; boundary=")
    # The single part is named "file" and carries the asset under its filename.
    assert b'name="file"' in req.data
    assert b'filename="auth.json"' in req.data


def test_build_request_body_carries_the_asset_bytes(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    # The opaque asset bytes ride in the multipart body verbatim.
    assert ASSET_BODY in req.data


def test_build_request_uses_basic_auth_with_platform_credentials(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    auth = req.get_header("Authorization")
    assert auth.startswith("Basic ")
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    assert decoded == "%s:%s" % (PLATFORM_ID, PLATFORM_SECRET)


def test_platform_credentials_only_in_header_never_in_body(tmp_path):
    # The crux of the asset-vs-credentials separation: the Platform secret lives in
    # the Authorization header ONLY, never in the multipart body alongside the asset.
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    assert PLATFORM_SECRET.encode("utf-8") not in req.data
    # And conversely, the asset body is NOT in the auth header.
    assert "auth.json" not in req.get_header("Authorization")


def test_build_request_empty_test_id_fails(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    with pytest.raises(ua.AuthAssetError) as exc:
        ua.build_upload_request("   ", asset, _platform_credentials())
    assert "test-id" in str(exc.value)


def test_build_request_honors_base_url_override(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(
        TEST_ID, asset, _platform_credentials(), base_url="https://example.test/api/v4/"
    )
    assert req.full_url == "https://example.test/api/v4/tests/%s/files" % TEST_ID


# --- sending: success / rejection via a mocked transport ---------------------


def test_upload_success_reports_ok(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    opener = _RecordingOpener(_FakeResponse(201))
    result = ua.upload_auth_asset(TEST_ID, asset, _platform_credentials(), opener=opener)
    assert result.ok is True
    assert result.status == 201
    assert result.filename == "auth.json"
    # The mocked transport really did receive the built request.
    assert opener.request.full_url.endswith("/tests/%s/files" % TEST_ID)


def test_upload_http_error_reports_not_ok(tmp_path):
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    err = urllib.error.HTTPError(
        url="https://a.blazemeter.com/api/v4/tests/%s/files" % TEST_ID,
        code=422,
        msg="Unprocessable",
        hdrs=None,
        fp=None,
    )
    opener = _RecordingOpener(error=err)
    result = ua.upload_auth_asset(TEST_ID, asset, _platform_credentials(), opener=opener)
    assert result.ok is False
    assert result.status == 422


# --- credential reuse via injected env (no real environment touched) ---------


def test_cli_resolves_platform_credentials_from_injected_env(tmp_path, monkeypatch, capsys):
    _write_asset(tmp_path)
    captured = {}

    def fake_upload(test_id, asset, credentials, **kwargs):
        # The CLI must hand the upload the credentials it resolved from env.
        captured["creds"] = credentials.as_dict()
        captured["asset_content"] = asset.content
        return ua.UploadResult(test_id=str(test_id), filename=asset.filename, status=201, ok=True)

    monkeypatch.setattr(ua, "upload_auth_asset", fake_upload)
    env = {"API_KEY_ID": PLATFORM_ID, "API_KEY_SECRET": PLATFORM_SECRET}
    code = ua.main(["--test-id", TEST_ID, "--artifacts-dir", str(tmp_path)], env=env)
    out = capsys.readouterr()
    assert code == 0
    assert captured["creds"] == {"id": PLATFORM_ID, "secret": PLATFORM_SECRET}
    # The asset content passed through opaquely.
    assert captured["asset_content"] == ASSET_BODY
    # Nothing sensitive leaked to stdout/stderr.
    assert PLATFORM_SECRET not in out.out and PLATFORM_SECRET not in out.err


def test_cli_missing_asset_exits_without_resolving_credentials(tmp_path, monkeypatch, capsys):
    # If the asset isn't there, fail clearly — and never even reach credential use.
    def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("credentials should not be resolved when asset is missing")

    monkeypatch.setattr(ua, "resolve_credentials", boom)
    code = ua.main(
        ["--test-id", TEST_ID, "--artifacts-dir", str(tmp_path)],
        env={"API_KEY_ID": PLATFORM_ID, "API_KEY_SECRET": PLATFORM_SECRET},
    )
    out = capsys.readouterr()
    assert code == 2
    assert "auth.json" in out.err


def test_cli_unconfigured_credentials_fail_cleanly(tmp_path, capsys):
    _write_asset(tmp_path)
    code = ua.main(["--test-id", TEST_ID, "--artifacts-dir", str(tmp_path)], env={})
    out = capsys.readouterr()
    assert code == 1
    assert "error" in out.err.lower()


# --- the safety contract: no Platform secret leaks anywhere -------------------


def test_no_platform_secret_in_success_output(tmp_path, monkeypatch, capsys):
    _write_asset(tmp_path)
    monkeypatch.setattr(
        ua,
        "upload_auth_asset",
        lambda test_id, asset, credentials, **k: ua.UploadResult(
            test_id=str(test_id), filename=asset.filename, status=201, ok=True
        ),
    )
    env = {"API_KEY_ID": PLATFORM_ID, "API_KEY_SECRET": PLATFORM_SECRET}
    ua.main(["--test-id", TEST_ID, "--path", str(tmp_path / "auth.json")], env=env)
    out = capsys.readouterr()
    assert PLATFORM_SECRET not in out.out
    assert PLATFORM_SECRET not in out.err


def test_rejection_message_omits_secret_and_asset_body(tmp_path, monkeypatch, capsys):
    _write_asset(tmp_path)
    monkeypatch.setattr(
        ua,
        "upload_auth_asset",
        lambda test_id, asset, credentials, **k: ua.UploadResult(
            test_id=str(test_id), filename=asset.filename, status=403, ok=False
        ),
    )
    env = {"API_KEY_ID": PLATFORM_ID, "API_KEY_SECRET": PLATFORM_SECRET}
    code = ua.main(["--test-id", TEST_ID, "--artifacts-dir", str(tmp_path)], env=env)
    out = capsys.readouterr()
    assert code == 1
    assert PLATFORM_SECRET not in out.err
    # The asset's contents must not be echoed either.
    assert "sut_token" not in out.err


def test_basic_auth_header_value_is_not_returned_for_logging(tmp_path):
    # Sanity: there is no helper that hands back the Platform secret as plain text.
    asset = ua.locate_auth_asset(explicit_path=str(_write_asset(tmp_path)))
    req = ua.build_upload_request(TEST_ID, asset, _platform_credentials())
    # repr of the Request must not expose the decoded secret.
    assert PLATFORM_SECRET not in repr(req)
