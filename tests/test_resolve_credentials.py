"""Fixture-driven tests for BlazeMeter credential resolution.

Credential resolution is deterministic, security-sensitive logic, so it gets real
tests over its *external behaviour*: given an environment (and maybe a key file on
disk), which credentials come back, and — just as important — that secrets never
leak into a ``repr``, log line, or error message.

The resolver reads from an injected ``env`` mapping, so these tests never touch the
real process environment; key files are written under pytest's ``tmp_path``.
"""

import json

import pytest

import resolve_credentials as rc

# Obviously-fake, low-entropy placeholder values — NOT real credentials. Deliberately
# kept un-secret-like (no random/high-entropy tokens) so secret scanners don't flag the
# fixtures; the tests only need a distinctive string to assert presence/absence of.
SECRET = "example-not-a-real-secret-value"
KEY_ID = "example-not-a-real-key-id"


def _write_key_file(tmp_path, payload, name="api-key.json"):
    path = tmp_path / name
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return str(path)


# --- precedence: env pair wins -----------------------------------------------


def test_env_pair_when_both_set():
    creds = rc.resolve_credentials({"API_KEY_ID": KEY_ID, "API_KEY_SECRET": SECRET})
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}
    assert creds.source == "env"


def test_env_pair_is_whitespace_trimmed():
    creds = rc.resolve_credentials(
        {"API_KEY_ID": "  %s  " % KEY_ID, "API_KEY_SECRET": "  %s  " % SECRET}
    )
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}


def test_env_pair_takes_precedence_over_key_file(tmp_path):
    # Both sources configured → the pair wins, the file is not consulted.
    key_file = _write_key_file(tmp_path, {"id": "file-id", "secret": "file-secret"})
    creds = rc.resolve_credentials(
        {"API_KEY_ID": KEY_ID, "API_KEY_SECRET": SECRET, "BLAZEMETER_API_KEY": key_file}
    )
    assert creds.source == "env"
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}


# --- precedence: fall back to the key file -----------------------------------


def test_key_file_when_pair_absent(tmp_path):
    key_file = _write_key_file(tmp_path, {"id": KEY_ID, "secret": SECRET})
    creds = rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}
    assert creds.source.startswith("file:")


def test_key_file_used_when_pair_only_partially_set(tmp_path):
    # Only API_KEY_ID set (no secret) → the pair is incomplete, so fall through.
    key_file = _write_key_file(tmp_path, {"id": KEY_ID, "secret": SECRET})
    creds = rc.resolve_credentials(
        {"API_KEY_ID": "ignored-partial", "BLAZEMETER_API_KEY": key_file}
    )
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}
    assert creds.source.startswith("file:")


def test_key_file_values_are_whitespace_trimmed(tmp_path):
    key_file = _write_key_file(tmp_path, {"id": "  %s  " % KEY_ID, "secret": "  %s\n" % SECRET})
    creds = rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert creds.as_dict() == {"id": KEY_ID, "secret": SECRET}


# --- partial / missing credentials fail clearly ------------------------------


def test_no_sources_configured_fails():
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({})
    msg = str(exc.value)
    assert "API_KEY_ID" in msg and "BLAZEMETER_API_KEY" in msg


def test_partial_pair_no_file_fails_with_hint():
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"API_KEY_ID": KEY_ID})
    # The message should hint which half is missing, by NAME (never the value).
    msg = str(exc.value)
    assert "API_KEY_ID" in msg
    assert KEY_ID not in msg


def test_empty_string_env_vars_are_treated_as_unset():
    with pytest.raises(rc.CredentialError):
        rc.resolve_credentials({"API_KEY_ID": "", "API_KEY_SECRET": "", "BLAZEMETER_API_KEY": ""})


# --- malformed key file fails clearly ----------------------------------------


def test_key_file_path_missing_fails(tmp_path):
    missing = str(tmp_path / "nope.json")
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": missing})
    assert "not a readable file" in str(exc.value)


def test_key_file_invalid_json_fails(tmp_path):
    key_file = _write_key_file(tmp_path, "this is { not json")
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert "not valid JSON" in str(exc.value)


def test_key_file_not_an_object_fails(tmp_path):
    key_file = _write_key_file(tmp_path, json.dumps(["id", "secret"]))
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert "JSON object" in str(exc.value)


def test_key_file_missing_secret_fails(tmp_path):
    key_file = _write_key_file(tmp_path, {"id": KEY_ID})
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    msg = str(exc.value)
    assert "secret" in msg and "missing" in msg


def test_key_file_empty_id_fails(tmp_path):
    key_file = _write_key_file(tmp_path, {"id": "   ", "secret": SECRET})
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert "id" in str(exc.value)


# --- the safety contract: secrets never leak ---------------------------------


def test_repr_redacts_id_and_secret():
    creds = rc.Credentials(id=KEY_ID, secret=SECRET, source="env")
    text = repr(creds)
    assert SECRET not in text
    assert KEY_ID not in text
    assert "redacted" in text
    # str() must be just as safe as repr() (e.g. f-strings, %s logging).
    assert SECRET not in str(creds)
    assert SECRET not in "%s" % creds


def test_malformed_key_file_error_never_echoes_contents(tmp_path):
    # A key file that *is* valid JSON but holds the secret in the wrong shape must
    # not have that secret surface in the error text.
    key_file = _write_key_file(tmp_path, {"api_key_secret": SECRET})
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert SECRET not in str(exc.value)


def test_invalid_json_error_never_echoes_file_body(tmp_path):
    # Invalid JSON whose raw bytes contain the secret must not be echoed back.
    key_file = _write_key_file(tmp_path, '{ "secret": "%s" ' % SECRET)  # truncated → invalid
    with pytest.raises(rc.CredentialError) as exc:
        rc.resolve_credentials({"BLAZEMETER_API_KEY": key_file})
    assert SECRET not in str(exc.value)


# --- CLI surface (the --help smoke target) -----------------------------------


def test_cli_check_succeeds_and_redacts(tmp_path, monkeypatch, capsys):
    key_file = _write_key_file(tmp_path, {"id": KEY_ID, "secret": SECRET})
    monkeypatch.setenv("BLAZEMETER_API_KEY", key_file)
    monkeypatch.delenv("API_KEY_ID", raising=False)
    monkeypatch.delenv("API_KEY_SECRET", raising=False)
    code = rc.main(["check"])
    out = capsys.readouterr()
    assert code == 0
    assert SECRET not in out.out and SECRET not in out.err


def test_cli_check_fails_cleanly_when_unconfigured(monkeypatch, capsys):
    for var in ("API_KEY_ID", "API_KEY_SECRET", "BLAZEMETER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    code = rc.main(["check"])
    out = capsys.readouterr()
    assert code == 1
    assert "error" in out.err.lower()
