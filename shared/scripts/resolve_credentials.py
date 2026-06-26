#!/usr/bin/env python3
"""Resolve BlazeMeter Platform Credentials, reusing the MCP's env vars.

Any shared script or skill that calls the BlazeMeter REST API directly needs an
API key id + secret. We deliberately reuse the *exact* scheme the BlazeMeter MCP
already defines (see `docs/adr/0008-credentials-reuse-mcp-env-vars.md`), so a user
who already has the MCP working needs no extra setup. Resolution order:

  1. ``API_KEY_ID`` + ``API_KEY_SECRET`` (env) — used only if **both** are set; else
  2. ``BLAZEMETER_API_KEY`` (env) — a path to a JSON key file ``{"id", "secret"}``.

There is no repo-specific config and no hardcoded default key-file path. If neither
source is configured (or the key file is malformed), resolution fails with a clear
error that names the *sources* it checked but **never** echoes a credential value or
the contents of the key file.

Safety contract (the whole reason this is one small, tested component):

  * Resolved credentials are returned for use but are **never logged, printed, or
    embedded in output**. ``Credentials`` redacts its id and secret in ``repr()`` so
    even an accidental ``print(creds)`` / log call cannot leak them.
  * Error messages reference env-var names and the key-file *path*, never values.

This module is dependency-free (Python standard library only) so the CI lint and
``--help`` smoke steps need nothing installed. It exposes ``resolve_credentials()``
and ``Credentials`` for skills and tests, and a small CLI for CI / manual checks.

Usage:
    python resolve_credentials.py            # check that credentials resolve (redacted)
    python resolve_credentials.py check      # same, explicit
    python resolve_credentials.py --help
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

ENV_KEY_ID = "API_KEY_ID"
ENV_KEY_SECRET = "API_KEY_SECRET"
ENV_KEY_FILE = "BLAZEMETER_API_KEY"

_REDACTED = "***redacted***"


class CredentialError(Exception):
    """Raised when credentials cannot be resolved.

    Messages name the sources that were checked (env-var names, key-file path) but
    deliberately never include a credential value or key-file contents.
    """


@dataclass(frozen=True)
class Credentials:
    """A resolved BlazeMeter API key.

    ``source`` records *how* it was resolved (e.g. ``"env"`` or ``"file:/p/k.json"``)
    for transparent, non-sensitive logging. ``repr`` redacts ``id`` and ``secret`` so
    the object is safe to log, print, or drop into an exception by accident.
    """

    id: str
    secret: str
    source: str

    def __repr__(self) -> str:  # pragma: no cover - exercised via tests asserting redaction
        return "Credentials(id=%s, secret=%s, source=%r)" % (_REDACTED, _REDACTED, self.source)

    __str__ = __repr__

    def as_dict(self) -> dict[str, str]:
        """Return ``{"id", "secret"}`` for the caller that actually needs the values."""
        return {"id": self.id, "secret": self.secret}


def _resolve_from_env_pair(env: Mapping[str, str]) -> Credentials | None:
    """Use ``API_KEY_ID`` + ``API_KEY_SECRET`` only if *both* are non-empty."""
    key_id = (env.get(ENV_KEY_ID) or "").strip()
    secret = (env.get(ENV_KEY_SECRET) or "").strip()
    if key_id and secret:
        return Credentials(id=key_id, secret=secret, source="env")
    return None


def _resolve_from_key_file(path_str: str) -> Credentials:
    """Read ``{"id", "secret"}`` from the JSON key file at ``path_str``.

    Raises ``CredentialError`` (never leaking file contents) on a missing file,
    invalid JSON, wrong shape, or empty id/secret.
    """
    path = Path(path_str).expanduser()
    if not path.is_file():
        raise CredentialError(
            "%s points to %r, which is not a readable file" % (ENV_KEY_FILE, str(path))
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        # Report the OS error class/strerror, not the file body.
        raise CredentialError(
            "could not read the key file at %r (%s)" % (str(path), exc.strerror or "unreadable")
        ) from None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Deliberately swallow the original message — it can quote the file body.
        raise CredentialError(
            "the key file at %r is not valid JSON (expected an object with "
            '"id" and "secret")' % str(path)
        ) from None

    if not isinstance(data, dict):
        raise CredentialError(
            'the key file at %r must be a JSON object with "id" and "secret"' % str(path)
        )

    key_id = data.get("id")
    secret = data.get("secret")
    missing = [k for k in ("id", "secret") if not (isinstance(data.get(k), str) and data.get(k).strip())]
    if missing:
        raise CredentialError(
            "the key file at %r is missing non-empty %s"
            % (str(path), " and ".join(repr(m) for m in missing))
        )

    return Credentials(id=key_id.strip(), secret=secret.strip(), source="file:%s" % path)


def resolve_credentials(env: Mapping[str, str] | None = None) -> Credentials:
    """Resolve BlazeMeter Platform Credentials following the documented precedence.

    Parameters
    ----------
    env:
        Environment mapping to read (defaults to ``os.environ``). Injectable so tests
        never have to mutate the real process environment.

    Returns
    -------
    Credentials
        The resolved ``{id, secret}`` plus a non-sensitive ``source`` label.

    Raises
    ------
    CredentialError
        If neither source is configured, or the key file is malformed. The message
        names the sources checked but never includes a credential value.
    """
    if env is None:
        env = os.environ

    from_env = _resolve_from_env_pair(env)
    if from_env is not None:
        return from_env

    key_file = (env.get(ENV_KEY_FILE) or "").strip()
    if key_file:
        return _resolve_from_key_file(key_file)

    # Nothing usable. Give a precise, value-free diagnosis of what was found.
    partial = [
        name for name in (ENV_KEY_ID, ENV_KEY_SECRET) if (env.get(name) or "").strip()
    ]
    hint = ""
    if partial:
        hint = (
            " (%s is set but its partner is not — the pair needs both)"
            % " and ".join(partial)
        )
    raise CredentialError(
        "no BlazeMeter credentials found: set %s + %s, or %s to a JSON key file%s"
        % (ENV_KEY_ID, ENV_KEY_SECRET, ENV_KEY_FILE, hint)
    )


def _mask(value: str) -> str:
    """Mask a credential for display: keep the last 4 chars, redact the rest."""
    if len(value) <= 4:
        return "****"
    return "****" + value[-4:]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Resolve BlazeMeter Platform Credentials (redacted) from the "
        "BlazeMeter MCP's environment variables.",
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="check",
        choices=["check"],
        help="check: verify credentials resolve and print a redacted confirmation "
        "(default).",
    )
    args = parser.parse_args(argv)

    # Only one action today; argparse keeps the surface ready for more.
    assert args.action == "check"
    try:
        creds = resolve_credentials()
    except CredentialError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    # Never print the secret; show only the source and a masked id.
    print("Resolved BlazeMeter credentials via %s (id %s)." % (creds.source, _mask(creds.id)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
