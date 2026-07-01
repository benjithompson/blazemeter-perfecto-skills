"""Fixture-driven tests for the pure baseline logic (`bzm_baseline`).

These exercise the deterministic half of the `bzm-baseline` skill with no
live BlazeMeter calls — exactly the pieces ADR-0017 makes the skill responsible for:

  * resolving the active baseline (explicit pin wins over last-passing);
  * "last passing run" selection across multiple runs (newest passing wins);
  * the no-passing-run case;
  * a malformed / missing committed baseline file.

Tests assert external behaviour: given inputs, what the functions return.
"""

import json

import pytest

import bzm_baseline as bb


# --- last-passing selection --------------------------------------------------


def _exec(id_, status, end_time):
    return {"id": id_, "status": status, "end_time": end_time}


def test_last_passing_picks_most_recent_passing_run():
    executions = [
        _exec("100", "passed", 1000),
        _exec("200", "failed", 3000),  # newer, but failed → excluded
        _exec("150", "passed", 2000),  # newest passing → should win
        _exec("120", "passed", 1500),
    ]
    chosen = bb.select_last_passing(executions)
    assert chosen is not None
    assert chosen["id"] == "150"


def test_last_passing_accepts_pass_and_passed_status_spellings():
    executions = [_exec("1", "PASS", 500), _exec("2", "Passed", 900)]
    assert bb.select_last_passing(executions)["id"] == "2"


def test_last_passing_excludes_non_passing_statuses():
    executions = [
        _exec("1", "failed", 1000),
        _exec("2", "aborted", 2000),
        _exec("3", "error", 3000),
        _exec("4", "noData", 4000),
        _exec("5", "unset", 5000),
    ]
    assert bb.select_last_passing(executions) is None


def test_no_passing_run_available_returns_none():
    assert bb.select_last_passing([]) is None
    assert bb.select_last_passing([_exec("1", "failed", 10)]) is None


def test_last_passing_breaks_ties_deterministically_by_id():
    # Same end_time → fall back to id so selection is stable run to run.
    executions = [_exec("aaa", "passed", 1000), _exec("bbb", "passed", 1000)]
    assert bb.select_last_passing(executions)["id"] == "bbb"


def test_last_passing_does_not_mutate_input():
    executions = [_exec("1", "passed", 10), _exec("2", "passed", 20)]
    snapshot = json.loads(json.dumps(executions))
    bb.select_last_passing(executions)
    assert executions == snapshot


# --- resolve: explicit pin wins over last-passing ----------------------------


def test_resolve_prefers_explicit_pin_over_last_passing():
    baseline = {"12345": "98765"}
    executions = [_exec("55555", "passed", 9999)]  # newer passing run, ignored
    result = bb.resolve_baseline(baseline, "12345", executions)
    assert result == {"source": "pinned", "execution_id": "98765"}


def test_resolve_falls_back_to_last_passing_when_not_pinned():
    baseline = {"67890": "111"}  # different test pinned
    executions = [_exec("200", "passed", 2000), _exec("100", "passed", 1000)]
    result = bb.resolve_baseline(baseline, "12345", executions)
    assert result == {"source": "last-passing", "execution_id": "200"}


def test_resolve_returns_none_with_no_pin_and_no_passing_run():
    result = bb.resolve_baseline({}, "12345", [_exec("1", "failed", 1)])
    assert result == {"source": "none", "execution_id": None}


def test_resolve_normalizes_int_test_id_against_string_keyed_file():
    baseline = {"12345": "98765"}
    result = bb.resolve_baseline(baseline, 12345)
    assert result == {"source": "pinned", "execution_id": "98765"}


# --- the committed CI file: parse / merge / serialize / round-trip -----------


def test_parse_baseline_normalizes_ids_to_strings():
    parsed = bb.parse_baseline('{"12345": 98765}')
    assert parsed == {"12345": "98765"}


def test_parse_empty_file_is_empty_baseline():
    assert bb.parse_baseline("") == {}
    assert bb.parse_baseline("   \n ") == {}


def test_malformed_baseline_json_raises():
    with pytest.raises(bb.BaselineError):
        bb.parse_baseline("{ not json ]")


def test_malformed_baseline_non_object_raises():
    with pytest.raises(bb.BaselineError):
        bb.parse_baseline('["12345", "98765"]')


def test_malformed_baseline_nested_value_raises():
    with pytest.raises(bb.BaselineError):
        bb.parse_baseline('{"12345": {"execution_id": "98765"}}')


def test_missing_baseline_file_is_empty_not_error(tmp_path):
    assert bb.load_baseline(tmp_path / "does-not-exist.json") == {}


def test_load_present_but_malformed_file_raises(tmp_path):
    p = tmp_path / "baseline.json"
    p.write_text("{ broken")
    with pytest.raises(bb.BaselineError):
        bb.load_baseline(p)


def test_merge_preserves_other_entries_and_does_not_mutate():
    original = {"67890": "99001"}
    merged = bb.merge_baseline(original, "12345", "98765")
    assert merged == {"67890": "99001", "12345": "98765"}
    assert original == {"67890": "99001"}  # input untouched


def test_merge_rejects_empty_ids():
    with pytest.raises(bb.BaselineError):
        bb.merge_baseline({}, "", "98765")
    with pytest.raises(bb.BaselineError):
        bb.merge_baseline({}, "12345", "")


def test_serialize_is_sorted_and_round_trips(tmp_path):
    baseline = {"67890": "99001", "12345": "98765"}
    text = bb.serialize_baseline(baseline)
    # Sorted keys keep the committed diff minimal.
    assert text.index('"12345"') < text.index('"67890"')
    p = tmp_path / ".blazemeter" / "baseline.json"
    bb.write_baseline(p, baseline)
    assert bb.load_baseline(p) == baseline


def test_diff_shows_the_change_before_writing():
    old = {"12345": "98765"}
    new = bb.merge_baseline(old, "12345", "99999")
    diff = bb.diff_baseline(old, new)
    assert "-" in diff and "+" in diff
    assert "99999" in diff


def test_write_creates_blazemeter_dir(tmp_path):
    p = tmp_path / ".blazemeter" / "baseline.json"
    bb.write_baseline(p, {"12345": "98765"})
    assert p.exists()


# --- CLI smoke ---------------------------------------------------------------


def test_cli_help_returns_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        bb.main(["--help"])
    assert exc.value.code == 0


def test_cli_set_is_dry_run_by_default(tmp_path, capsys):
    p = tmp_path / "baseline.json"
    rc = bb.main(["set", "--file", str(p), "--test-id", "12345", "--execution-id", "98765"])
    assert rc == 0
    assert not p.exists()  # dry run wrote nothing
    assert "dry run" in capsys.readouterr().out


def test_cli_set_with_write_creates_file(tmp_path):
    p = tmp_path / "baseline.json"
    rc = bb.main(
        ["set", "--file", str(p), "--test-id", "12345", "--execution-id", "98765", "--write"]
    )
    assert rc == 0
    assert bb.load_baseline(p) == {"12345": "98765"}
