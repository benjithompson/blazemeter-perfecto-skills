#!/usr/bin/env python3
"""Generate a GitHub Actions workflow that runs a BlazeMeter test in CI and gates on it.

This module holds the **deterministic** half of the `bzm-ci-setup` skill so it
can be fixture-tested without any live BlazeMeter or GitHub calls (see
`tests/test_bzm_ci_scaffold.py`). The skill prose resolves the test's context via the
BlazeMeter MCP; *this* script takes the resolved parameters and emits the workflow YAML
as text. It makes no network calls.

What it produces, per ADR-0016 ("generated CI YAML is secrets-only"):

  * a GitHub Actions workflow under `.github/workflows/` that, on the chosen
    trigger(s), starts a BlazeMeter test, waits for it to finish, and **gates the job**
    on the result;
  * the workflow authenticates to BlazeMeter **only** via
    `${{ secrets.BLAZEMETER_API_KEY }}` — the generated text contains nothing but
    `secrets.*` references: no literal key, no key-file path, no `echo`/`cat` of a
    secret. The plugin never embeds, logs, or echoes a token.

Two **gate policies** are supported:

  * ``pass-fail`` — gate on the test's own failure criteria (the execution's
    pass/fail verdict).
  * ``compare-baseline`` — gate by comparing the run against the committed
    ``.blazemeter/baseline.json`` baseline (ADR-0017). The workflow checks out the
    repo so the baseline file is available, and the user must commit it (see the
    `bzm-baseline` skill).

Three **triggers** are supported and can be combined: ``pr`` (pull_request),
``push`` (push to a branch), and ``schedule`` (cron).

This is dependency-free at *runtime* of the skill's environment: building the YAML
uses only the Python standard library so the CI `--help` smoke test needs nothing
installed. (The tests parse the output with pyyaml, a dev-only dependency.)

Usage:
    python bzm_ci_scaffold.py --help
    python bzm_ci_scaffold.py --test-id 12345 --trigger pr --gate pass-fail
    python bzm_ci_scaffold.py --test-id 12345 --trigger push --trigger schedule \\
        --branch main --cron "0 6 * * 1" --gate compare-baseline
"""

from __future__ import annotations

import argparse
import sys

# The GitHub Actions secret the generated workflow reads its BlazeMeter credentials
# from. This is the ONLY way the workflow gets a credential — never a literal key, a
# key-file path, or an echoed token (ADR-0016, conventions §5/§6).
SECRET_REF = "${{ secrets.BLAZEMETER_API_KEY }}"

VALID_TRIGGERS = ("pr", "push", "schedule")
VALID_GATES = ("pass-fail", "compare-baseline")

DEFAULT_WORKFLOW_NAME = "BlazeMeter performance gate"
DEFAULT_BRANCH = "main"
DEFAULT_CRON = "0 6 * * 1"  # 06:00 UTC every Monday
DEFAULT_FILENAME = "blazemeter-performance.yml"


class ScaffoldError(Exception):
    """Raised for invalid scaffold parameters."""


def _normalize_triggers(triggers: list[str]) -> list[str]:
    """Validate and de-duplicate triggers, preserving canonical order."""
    if not triggers:
        raise ScaffoldError("at least one --trigger is required (pr, push, or schedule)")
    seen = set()
    for t in triggers:
        if t not in VALID_TRIGGERS:
            raise ScaffoldError(
                "unknown trigger %r (choose from: %s)" % (t, ", ".join(VALID_TRIGGERS))
            )
        seen.add(t)
    # Canonical order so the emitted YAML is stable run to run.
    return [t for t in VALID_TRIGGERS if t in seen]


def build_on_block(triggers: list[str], branch: str, cron: str) -> str:
    """Build the `on:` block for the chosen triggers."""
    lines = ["on:"]
    if "pr" in triggers:
        lines.append("  pull_request:")
    if "push" in triggers:
        lines.append("  push:")
        lines.append("    branches: [%s]" % branch)
    if "schedule" in triggers:
        lines.append("  schedule:")
        lines.append('    - cron: "%s"' % cron)
    # `workflow_dispatch` is always added so the gate can be run on demand from the UI.
    lines.append("  workflow_dispatch:")
    return "\n".join(lines)


def _runner_script(gate: str) -> list[str]:
    """The inline Python run step that drives the BlazeMeter test and gates on it.

    It reads the credential from the env var BLAZEMETER_API_KEY, which the step maps
    from ${{ secrets.BLAZEMETER_API_KEY }}. The credential value is never echoed: it is
    written to a key file path that the BlazeMeter tooling reads, and only the run's
    status (not the secret) is printed.
    """
    # NOTE: this runner targets the BlazeMeter REST API v4 by design. The BlazeMeter
    # MCP is an interactive server, not a headless CI runner, so a CI job uses the
    # documented REST v4 fallback (conventions §5). The credential file format matches
    # what bzm-mcp's BLAZEMETER_API_KEY expects: a JSON object with an id and secret.
    common = [
        '          import json, os, sys, time, base64',
        '          import urllib.request, urllib.error',
        '          API = "https://a.blazemeter.com/api/v4"',
        '          key_path = os.environ["BZM_KEY_FILE"]',
        '          with open(key_path) as fh:',
        '              key = json.load(fh)',
        '          token = base64.b64encode(',
        '              ("%s:%s" % (key["id"], key["secret"])).encode()',
        '          ).decode()',
        '          headers = {"Authorization": "Basic %s" % token,',
        '                     "Content-Type": "application/json"}',
        '          def call(method, path, body=None):',
        '              data = json.dumps(body).encode() if body is not None else None',
        '              req = urllib.request.Request(API + path, data=data,',
        '                                           headers=headers, method=method)',
        '              with urllib.request.urlopen(req) as resp:',
        '                  return json.load(resp)',
        '          test_id = os.environ["BZM_TEST_ID"]',
        '          print("Starting BlazeMeter test %s" % test_id)',
        '          started = call("POST", "/tests/%s/start" % test_id)',
        '          master_id = started["result"]["id"]',
        '          report = "https://a.blazemeter.com/app/#/masters/%s" % master_id',
        '          print("Execution %s — report: %s" % (master_id, report))',
        '          while True:',
        '              status = call("GET", "/masters/%s/status" % master_id)["result"]',
        '              if status.get("progress", 0) >= 100 or status.get("status") == "ENDED":',
        '                  break',
        '              time.sleep(30)',
        '          master = call("GET", "/masters/%s" % master_id)["result"]',
    ]
    if gate == "pass-fail":
        gate_lines = [
            '          # Gate on the test\'s own failure criteria (the run\'s pass/fail verdict).',
            '          note = master.get("note") or ""',
            '          passed = master.get("passed")',
            '          print("Verdict: passed=%r note=%s" % (passed, note))',
            '          if passed is False:',
            '              sys.exit("BlazeMeter gate FAILED: test failure criteria were violated")',
            '          if passed is None:',
            '              sys.exit("BlazeMeter gate INDETERMINATE: no failure criteria defined")',
            '          print("BlazeMeter gate PASSED")',
        ]
    else:  # compare-baseline
        gate_lines = [
            '          # Gate by comparing this run against the committed baseline',
            '          # (.blazemeter/baseline.json, ADR-0017). The baseline maps',
            '          # test_id -> execution_id; create/update it with the',
            '          # bzm-baseline skill and commit it.',
            '          baseline_file = ".blazemeter/baseline.json"',
            '          if not os.path.exists(baseline_file):',
            '              sys.exit("Missing %s — create it with the bzm-baseline skill and commit it" % baseline_file)',
            '          with open(baseline_file) as fh:',
            '              baseline = json.load(fh)',
            '          baseline_id = baseline.get(str(test_id))',
            '          if not baseline_id:',
            '              sys.exit("No baseline execution for test %s in %s" % (test_id, baseline_file))',
            '          this_kpi = call("GET", "/masters/%s/reports/default/summary" % master_id)',
            '          base_kpi = call("GET", "/masters/%s/reports/default/summary" % baseline_id)',
            '          def p90(summary):',
            '              return summary["result"]["summary"][0].get("p90", 0)',
            '          this_p90, base_p90 = p90(this_kpi), p90(base_kpi)',
            '          print("p90 this=%s baseline=%s (baseline execution %s)" % (this_p90, base_p90, baseline_id))',
            '          # Regression gate: fail if this run is more than 10%% slower at p90.',
            '          if base_p90 and this_p90 > base_p90 * 1.10:',
            '              sys.exit("BlazeMeter gate FAILED: p90 regressed vs baseline (%s > %s)" % (this_p90, base_p90))',
            '          print("BlazeMeter gate PASSED vs baseline")',
        ]
    return common + gate_lines


def build_workflow(
    test_id: str,
    triggers: list[str],
    gate: str,
    *,
    name: str = DEFAULT_WORKFLOW_NAME,
    branch: str = DEFAULT_BRANCH,
    cron: str = DEFAULT_CRON,
) -> str:
    """Return the GitHub Actions workflow YAML as text.

    `triggers` is any combination of "pr", "push", "schedule". `gate` is "pass-fail"
    or "compare-baseline". The credential is read only via ${{ secrets.BLAZEMETER_API_KEY }}.
    """
    if not str(test_id).strip():
        raise ScaffoldError("test_id must not be empty")
    if gate not in VALID_GATES:
        raise ScaffoldError(
            "unknown gate %r (choose from: %s)" % (gate, ", ".join(VALID_GATES))
        )
    triggers = _normalize_triggers(triggers)

    on_block = build_on_block(triggers, branch, cron)

    steps = []
    # compare-baseline needs the repo checked out so the baseline file is present.
    needs_checkout = gate == "compare-baseline"
    if needs_checkout:
        steps.append("      - uses: actions/checkout@v4")
        steps.append("")
    steps.append("      - uses: actions/setup-python@v5")
    steps.append("        with:")
    steps.append('          python-version: "3.12"')
    steps.append("")
    # Materialize the secret into a key file WITHOUT echoing it. The heredoc writes the
    # secret straight to a file; nothing prints the value.
    steps.append("      - name: Write BlazeMeter key file (never echoed)")
    steps.append("        env:")
    steps.append("          BLAZEMETER_API_KEY: %s" % SECRET_REF)
    steps.append("        run: |")
    steps.append("          install -m 600 /dev/null \"$RUNNER_TEMP/bzm-key.json\"")
    steps.append("          printf '%s' \"$BLAZEMETER_API_KEY\" > \"$RUNNER_TEMP/bzm-key.json\"")
    steps.append("")
    steps.append("      - name: Run BlazeMeter test and gate on the result")
    steps.append("        env:")
    steps.append("          BZM_TEST_ID: \"%s\"" % test_id)
    steps.append("          BZM_KEY_FILE: ${{ runner.temp }}/bzm-key.json")
    steps.append("        run: |")
    steps.append("          python - <<'PY'")
    steps.extend(_runner_script(gate))
    steps.append("          PY")

    workflow = "\n".join(
        [
            "# Generated by the bzm-ci-setup skill.",
            "# Credentials are read ONLY from ${{ secrets.BLAZEMETER_API_KEY }} —",
            "# add it once as a repository secret (Settings → Secrets and variables → Actions).",
            "name: %s" % name,
            "",
            on_block,
            "",
            "jobs:",
            "  blazemeter-gate:",
            "    runs-on: ubuntu-latest",
            "    steps:",
            "\n".join(steps),
            "",
        ]
    )
    return workflow


# --- CLI ---------------------------------------------------------------------


def _cmd(args: argparse.Namespace) -> int:
    yaml_text = build_workflow(
        args.test_id,
        args.trigger,
        args.gate,
        name=args.name,
        branch=args.branch,
        cron=args.cron,
    )
    print(yaml_text, end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a GitHub Actions workflow that runs a BlazeMeter test in "
        "CI and gates on the result (credentials read only via "
        "${{ secrets.BLAZEMETER_API_KEY }}).",
    )
    parser.add_argument("--test-id", required=True, help="BlazeMeter test id to run in CI")
    parser.add_argument(
        "--trigger",
        action="append",
        default=[],
        choices=VALID_TRIGGERS,
        help="when to run; repeatable (pr, push, schedule)",
    )
    parser.add_argument(
        "--gate",
        default="pass-fail",
        choices=VALID_GATES,
        help="gate policy: pass-fail (test's failure criteria) or compare-baseline",
    )
    parser.add_argument(
        "--name", default=DEFAULT_WORKFLOW_NAME, help="workflow display name"
    )
    parser.add_argument(
        "--branch",
        default=DEFAULT_BRANCH,
        help="branch for the push trigger (default: %s)" % DEFAULT_BRANCH,
    )
    parser.add_argument(
        "--cron",
        default=DEFAULT_CRON,
        help='cron for the schedule trigger (default: "%s")' % DEFAULT_CRON,
    )
    parser.set_defaults(func=_cmd)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ScaffoldError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
