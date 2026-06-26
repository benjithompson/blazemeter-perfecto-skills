"""Live integration test for ``bzm_upload_auth`` against real BlazeMeter.

Gated: deselected by default (``-m "not live"``) and skips unless real Platform
Credentials resolve and ``BZM_LIVE_TEST_ID`` (a throwaway test to upload to) is set.
Run with ``pytest -m live``. See ``tests/conftest.py``.

This proves the real multipart upload to the v4 files endpoint, which the mocked unit
tests in ``test_bzm_upload_auth.py`` cannot. The uploaded ``auth.json`` is a harmless,
obviously-fake *test asset* — it is NOT Platform Credentials — and re-uploading it is
idempotent, so pointing this at a dedicated throwaway test leaves no meaningful residue.
"""

import json

import pytest

from bzm_upload_auth import locate_auth_asset, upload_auth_asset

pytestmark = pytest.mark.live


def test_uploads_auth_asset_to_real_test(live_credentials, require_live_env, tmp_path):
    test_id = require_live_env("BZM_LIVE_TEST_ID")

    # A throwaway *test asset* (authenticates a hypothetical system-under-test) — NOT the
    # BlazeMeter Platform Credentials. Deliberately low-entropy, obviously-fake content.
    asset_path = tmp_path / "auth.json"
    asset_path.write_text(
        json.dumps(
            {
                "note": "example test asset for live upload validation - not real credentials",
                "example_token": "example-system-under-test-token",
            }
        ),
        encoding="utf-8",
    )

    asset = locate_auth_asset(explicit_path=str(asset_path))
    result = upload_auth_asset(test_id, asset, live_credentials)

    assert result.ok, "upload of auth.json to test %s was rejected (HTTP %s)" % (
        test_id,
        result.status,
    )
    assert 200 <= result.status < 300
    assert result.filename == "auth.json"
