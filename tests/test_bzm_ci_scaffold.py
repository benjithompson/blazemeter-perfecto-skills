"""Fixture-driven tests for the CI-scaffold generator (`bzm_ci_scaffold`).

These exercise the deterministic half of the `bzm-ci-setup` skill with no
live BlazeMeter or GitHub calls — exactly the pieces ADR-0016 makes the skill
responsible for:

  * every supported trigger (pr / push / schedule) produces a well-formed `on:` block;
  * both gate policies (pass-fail / compare-baseline) produce a job that gates on the
    result, and compare-baseline checks out the repo to read .blazemeter/baseline.json;
  * the generated YAML reads credentials ONLY via ${{ secrets.BLAZEMETER_API_KEY }} and
    contains NO literal key/secret (ADR-0016, the security guarantee);
  * the output is valid YAML (parsed with pyyaml).

Tests assert external behaviour: given inputs, what the generator returns.
"""

import re

import pytest
import yaml

import bzm_ci_scaffold as cs

SECRET_REF = "${{ secrets.BLAZEMETER_API_KEY }}"


def _on_block(doc: dict) -> dict:
    """Return the `on:` mapping from a parsed workflow.

    PyYAML resolves the bare key `on` to the boolean True (YAML 1.1), so accept either.
    """
    if "on" in doc:
        return doc["on"]
    return doc[True]


# --- triggers ----------------------------------------------------------------


def test_pr_trigger_emits_pull_request():
    out = cs.build_workflow("12345", ["pr"], "pass-fail")
    on = _on_block(yaml.safe_load(out))
    assert "pull_request" in on
    assert "push" not in on and "schedule" not in on


def test_push_trigger_emits_push_with_branch():
    out = cs.build_workflow("12345", ["push"], "pass-fail", branch="develop")
    on = _on_block(yaml.safe_load(out))
    assert on["push"]["branches"] == ["develop"]


def test_schedule_trigger_emits_cron():
    out = cs.build_workflow("12345", ["schedule"], "pass-fail", cron="0 6 * * 1")
    on = _on_block(yaml.safe_load(out))
    assert on["schedule"] == [{"cron": "0 6 * * 1"}]


def test_triggers_can_be_combined_and_deduped_in_canonical_order():
    out = cs.build_workflow("12345", ["schedule", "pr", "pr", "push"], "pass-fail")
    on = _on_block(yaml.safe_load(out))
    assert set(on) >= {"pull_request", "push", "schedule"}


def test_workflow_dispatch_is_always_present():
    out = cs.build_workflow("12345", ["pr"], "pass-fail")
    assert "workflow_dispatch" in _on_block(yaml.safe_load(out))


def test_no_trigger_is_an_error():
    with pytest.raises(cs.ScaffoldError):
        cs.build_workflow("12345", [], "pass-fail")


def test_unknown_trigger_is_an_error():
    with pytest.raises(cs.ScaffoldError):
        cs.build_workflow("12345", ["nightly"], "pass-fail")


# --- gate policies -----------------------------------------------------------


def test_pass_fail_gate_does_not_check_out_repo():
    out = cs.build_workflow("12345", ["pr"], "pass-fail")
    # pass-fail gates on the run's verdict; it needs no repo file.
    assert "actions/checkout" not in out
    assert "failure criteria" in out


def test_compare_baseline_gate_checks_out_repo_and_reads_baseline_file():
    out = cs.build_workflow("12345", ["pr"], "compare-baseline")
    assert "actions/checkout@v4" in out
    assert ".blazemeter/baseline.json" in out


def test_unknown_gate_is_an_error():
    with pytest.raises(cs.ScaffoldError):
        cs.build_workflow("12345", ["pr"], "no-such-gate")


def test_empty_test_id_is_an_error():
    with pytest.raises(cs.ScaffoldError):
        cs.build_workflow("   ", ["pr"], "pass-fail")


# --- the security guarantee: secrets-only, no literal credential -------------


@pytest.mark.parametrize("trigger", ["pr", "push", "schedule"])
@pytest.mark.parametrize("gate", ["pass-fail", "compare-baseline"])
def test_credential_is_read_only_via_the_secret_ref(trigger, gate):
    out = cs.build_workflow("12345", [trigger], gate)
    assert SECRET_REF in out
    # The credential is consumed by reference; the secret value is never echoed/dumped.
    assert "echo $BLAZEMETER_API_KEY" not in out
    assert "echo \"$BLAZEMETER_API_KEY\"" not in out
    assert "cat $BZM_KEY_FILE" not in out
    assert "cat \"$BZM_KEY_FILE\"" not in out


# A literal BlazeMeter key file is JSON like {"id": "...", "secret": "..."} where the
# values are long hex/alphanumeric tokens. The generated YAML must contain no such
# literal value — only the secrets.* reference. This guards the ADR-0016 invariant.
_LITERAL_KEY_VALUE = re.compile(r'"(?:id|secret)"\s*:\s*"[A-Za-z0-9]{12,}"')


@pytest.mark.parametrize("trigger", ["pr", "push", "schedule"])
@pytest.mark.parametrize("gate", ["pass-fail", "compare-baseline"])
def test_no_literal_credential_in_generated_yaml(trigger, gate):
    out = cs.build_workflow("12345", [trigger], gate)
    assert not _LITERAL_KEY_VALUE.search(out), "generated YAML contains a literal credential"
    # Every `secrets.` reference is the approved one; no other secret is embedded.
    for m in re.findall(r"secrets\.\w+", out):
        assert m == "secrets.BLAZEMETER_API_KEY"


# --- valid YAML --------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["pr", "push", "schedule"])
@pytest.mark.parametrize("gate", ["pass-fail", "compare-baseline"])
def test_output_is_valid_yaml_with_a_gate_job(trigger, gate):
    out = cs.build_workflow("12345", [trigger], gate)
    doc = yaml.safe_load(out)
    assert isinstance(doc, dict)
    assert "blazemeter-gate" in doc["jobs"]
    assert doc["jobs"]["blazemeter-gate"]["runs-on"] == "ubuntu-latest"


def test_test_id_appears_in_the_workflow():
    out = cs.build_workflow("987654", ["pr"], "pass-fail")
    doc = yaml.safe_load(out)
    steps = doc["jobs"]["blazemeter-gate"]["steps"]
    env_values = [str(s.get("env", {})) for s in steps]
    assert any("987654" in v for v in env_values)


# --- CLI smoke ---------------------------------------------------------------


def test_cli_help_returns_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        cs.main(["--help"])
    assert exc.value.code == 0


def test_cli_emits_workflow_with_secret_ref(capsys):
    rc = cs.main(["--test-id", "12345", "--trigger", "pr", "--gate", "pass-fail"])
    assert rc == 0
    out = capsys.readouterr().out
    assert SECRET_REF in out
    assert yaml.safe_load(out)["jobs"]["blazemeter-gate"]


def test_cli_unknown_gate_exits_nonzero(capsys):
    with pytest.raises(SystemExit) as exc:
        cs.main(["--test-id", "12345", "--trigger", "pr", "--gate", "bogus"])
    # argparse rejects an invalid choice with exit code 2.
    assert exc.value.code == 2
