"""Shared plumbing for credential-gated **live** integration tests.

The unit tests for the BlazeMeter utilities mock the network (fast, hermetic, run
everywhere). These *live* tests instead hit the real BlazeMeter REST API to prove the
utilities work end-to-end — replacing the old "run it once by hand" manual-verify step.

They are **gated**, never noisy:

  * Every live test is marked ``@pytest.mark.live`` and is **deselected by default**
    (``addopts = -m "not live"`` in ``pyproject.toml``), so plain ``pytest`` and CI run
    only the mock tests. You opt in locally with ``pytest -m live``.
  * Even when selected, a live test **skips** (never fails) unless real Platform
    Credentials resolve *and* the target id it needs is configured. So ``pytest -m live``
    in an environment without credentials is all skips, not errors.

Configuration (any of these; env always wins over the file):

  * Credentials — reuse the BlazeMeter MCP's own scheme via the shared resolver:
    ``API_KEY_ID`` + ``API_KEY_SECRET``, or ``BLAZEMETER_API_KEY`` pointing at a JSON
    key file. As a convenience, a gitignored ``api-key.json`` at the repo root is picked
    up automatically.
  * Targets — ``BZM_LIVE_EXECUTION_ID`` (an execution/master that has artifacts, for the
    download test) and ``BZM_LIVE_TEST_ID`` (a throwaway test to upload an asset to, for
    the upload test).

For convenience these may live in a gitignored ``tests/live.env`` (``KEY=value`` lines);
see ``tests/live.env.example``. This file is loaded here without overriding anything
already set in the real environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resolve_credentials import CredentialError, resolve_credentials

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_ENV_FILE = Path(__file__).resolve().parent / "live.env"
DEFAULT_KEY_FILE = REPO_ROOT / "api-key.json"


def _load_live_env_file() -> None:
    """Load ``tests/live.env`` (if present) into the environment, never overriding.

    Lines are ``KEY=value``; blanks and ``#`` comments are ignored; surrounding quotes
    on the value are stripped. Values already set in the real environment win, so an
    explicit ``BZM_LIVE_TEST_ID=… pytest`` overrides the file.
    """
    if not LIVE_ENV_FILE.is_file():
        return
    for raw in LIVE_ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_live_env_file()


def _credentials_configured() -> bool:
    """True if some credential source is present (env pair, key-file var, or default file)."""
    if (os.environ.get("API_KEY_ID") or "").strip() and (os.environ.get("API_KEY_SECRET") or "").strip():
        return True
    if (os.environ.get("BLAZEMETER_API_KEY") or "").strip():
        return True
    return DEFAULT_KEY_FILE.is_file()


@pytest.fixture(scope="session")
def live_credentials():
    """Resolve real Platform Credentials, or **skip** the live test if none are configured.

    Reuses the shared resolver (the exact scheme the MCP uses). As a convenience, if no
    credential env var is set but a gitignored ``api-key.json`` exists at the repo root,
    it is used. Credentials are never logged: the resolver redacts them.
    """
    if (
        not ((os.environ.get("API_KEY_ID") or "").strip() and (os.environ.get("API_KEY_SECRET") or "").strip())
        and not (os.environ.get("BLAZEMETER_API_KEY") or "").strip()
        and DEFAULT_KEY_FILE.is_file()
    ):
        os.environ["BLAZEMETER_API_KEY"] = str(DEFAULT_KEY_FILE)

    try:
        return resolve_credentials()
    except CredentialError as exc:
        pytest.skip(
            "no BlazeMeter Platform Credentials for live tests (%s); set API_KEY_ID + "
            "API_KEY_SECRET or BLAZEMETER_API_KEY, or drop a gitignored api-key.json" % exc
        )


@pytest.fixture
def require_live_env():
    """Return a getter that yields a required live-target env var, or **skips** if unset."""

    def _require(name: str) -> str:
        value = (os.environ.get(name) or "").strip()
        if not value:
            pytest.skip("set %s to run this live test (see tests/live.env.example)" % name)
        return value

    return _require
