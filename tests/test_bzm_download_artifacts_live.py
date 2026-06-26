"""Live integration test for ``bzm_download_artifacts`` against real BlazeMeter.

Gated: deselected by default (``-m "not live"``) and skips unless real Platform
Credentials resolve and ``BZM_LIVE_EXECUTION_ID`` (an execution/master that produced
artifacts) is set. Run with ``pytest -m live``. See ``tests/conftest.py``.

This proves the whole REST path end-to-end — enumerate the execution's per-location
sessions, download each ``artifacts.zip``, and extract it — which the mocked unit tests
in ``test_bzm_download_artifacts.py`` cannot.
"""

import pytest

from bzm_download_artifacts import ARTIFACTS_ZIP, download_execution_artifacts

pytestmark = pytest.mark.live


def test_downloads_and_extracts_real_execution_artifacts(live_credentials, require_live_env, tmp_path):
    master_id = require_live_env("BZM_LIVE_EXECUTION_ID")

    # expected=[] so the per-location report doesn't flag "missing"; we assert on what
    # actually landed on disk instead, which is the real end-to-end signal.
    results = download_execution_artifacts(
        master_id, tmp_path, creds=live_credentials, expected=[]
    )

    assert results, "expected at least one session/location for execution %s" % master_id

    # A genuine failure (bad auth, HTTP/transport error) must fail the test. A session that
    # simply has no artifacts.zip (e.g. a functional/EUX session) is tolerated — the script
    # reports it per-location and we still expect at least one real download below.
    real_errors = [
        (r.location or r.session_id, r.error)
        for r in results
        if r.error and ("no %s" % ARTIFACTS_ZIP) not in r.error
    ]
    assert not real_errors, "unexpected download errors: %r" % real_errors

    # At least one location's artifacts.zip was written to disk...
    zips = list(tmp_path.rglob(ARTIFACTS_ZIP))
    assert zips, "no %s downloaded under %s (auth or execution problem?)" % (ARTIFACTS_ZIP, tmp_path)

    # ...and was actually extracted (a real archive yields inner files).
    extracted = [
        p for p in tmp_path.rglob("*") if p.is_file() and p.name != ARTIFACTS_ZIP
    ]
    assert extracted, "artifacts.zip downloaded but nothing was extracted from it"

    # At least one location is fully ok and recorded where it wrote its artifacts.
    assert any(r.ok and r.out_dir for r in results), "no location completed successfully"
