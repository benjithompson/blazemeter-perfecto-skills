#!/usr/bin/env python3
"""Pure baseline logic for the `bzm-set-baseline` skill.

This module holds the **deterministic** pieces of baseline handling so they can be
fixture-tested without any live BlazeMeter calls (see `tests/test_bzm_baseline.py`).
The live MCP reads (resolving context, listing executions, reading KPIs) stay in the
skill prose; everything here is pure and offline:

  * read / parse / merge / write the committed CI baseline file
    `.blazemeter/baseline.json`, a flat map of `test_id -> execution_id`
    (both stored as strings), per ADR-0017;
  * select the **last passing run** from a list of executions — the interactive
    default baseline when nothing is pinned, per ADR-0017.

Both halves are intentionally I/O-light and dependency-free (Python standard
library only) so the CI `--help` smoke test and the tests need nothing installed.

Usage:
    python bzm_baseline.py --help
    python bzm_baseline.py resolve  --file .blazemeter/baseline.json --test-id 12345
    python bzm_baseline.py last-passing  --executions runs.json
    python bzm_baseline.py set  --file .blazemeter/baseline.json \\
        --test-id 12345 --execution-id 98765 [--write]

`resolve` and `last-passing` are read-only. `set` prints a unified diff of the
baseline file and only writes when `--write` is passed — the skill always shows the
change before committing it.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

# Execution statuses that count as a "pass" for baseline selection. BlazeMeter's
# execution verdict is `execution_status`; only an explicit pass qualifies. An
# `unset`/`abort`/`error`/`noData` run is NOT a clean pass and must never become a
# baseline (consistent with how bzm-test-analysis treats those statuses).
PASSING_STATUSES = frozenset({"passed", "pass"})


class BaselineError(Exception):
    """Raised for a malformed baseline file or invalid input."""


# --- the committed CI baseline file: read / merge / write --------------------


def parse_baseline(text: str) -> dict[str, str]:
    """Parse `.blazemeter/baseline.json` text into a `{test_id: execution_id}` map.

    Both keys and values are normalized to strings (ids arrive as ints or strings
    from different callers). Raises `BaselineError` on anything that is not a flat
    JSON object of scalar -> scalar.
    """
    text = text.strip()
    if not text:
        # An empty file is treated as an empty (but valid) baseline map.
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise BaselineError("baseline file is not valid JSON: %s" % exc) from exc
    if not isinstance(data, dict):
        raise BaselineError(
            "baseline file must be a JSON object of test_id -> execution_id (got %s)"
            % type(data).__name__
        )
    result: dict[str, str] = {}
    for key, value in data.items():
        if isinstance(value, (dict, list, bool)) or value is None:
            raise BaselineError(
                "baseline entry for test_id %r must be a scalar execution id (got %r)"
                % (key, value)
            )
        result[str(key)] = str(value)
    return result


def load_baseline(path: str | Path) -> dict[str, str]:
    """Load a baseline file. A missing file is an empty baseline, not an error.

    A *present but malformed* file raises `BaselineError` — that is a real problem
    the user must see and fix, not silently swallowed.
    """
    path = Path(path)
    if not path.exists():
        return {}
    return parse_baseline(path.read_text(encoding="utf-8"))


def merge_baseline(
    baseline: dict[str, str], test_id: str | int, execution_id: str | int
) -> dict[str, str]:
    """Return a NEW map with `test_id` pointing at `execution_id`.

    Other entries are preserved untouched (one file gates many tests). The input
    map is not mutated.
    """
    test_id = str(test_id).strip()
    execution_id = str(execution_id).strip()
    if not test_id:
        raise BaselineError("test_id must not be empty")
    if not execution_id:
        raise BaselineError("execution_id must not be empty")
    merged = dict(baseline)
    merged[test_id] = execution_id
    return merged


def serialize_baseline(baseline: dict[str, str]) -> str:
    """Serialize a baseline map deterministically (sorted keys, trailing newline).

    Sorting keeps the committed file's diff minimal and review-friendly run to run.
    """
    return json.dumps(baseline, indent=2, sort_keys=True) + "\n"


def diff_baseline(
    old: dict[str, str], new: dict[str, str], path: str = ".blazemeter/baseline.json"
) -> str:
    """Unified diff between two baseline maps, for showing the change before writing."""
    old_text = serialize_baseline(old).splitlines(keepends=True)
    new_text = serialize_baseline(new).splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(old_text, new_text, fromfile="a/%s" % path, tofile="b/%s" % path)
    )


def write_baseline(path: str | Path, baseline: dict[str, str]) -> None:
    """Write the baseline map to disk, creating the `.blazemeter/` dir if needed."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_baseline(baseline), encoding="utf-8")


# --- "last passing run" selection --------------------------------------------


def _is_passing(execution: dict) -> bool:
    status = str(execution.get("status", "")).strip().lower()
    return status in PASSING_STATUSES


def _sort_key(execution: dict):
    # Most recent first: prefer end_time; fall back to id so ties are deterministic.
    end_time = execution.get("end_time")
    return (end_time if end_time is not None else float("-inf"), str(execution.get("id", "")))


def select_last_passing(executions: list[dict]) -> dict | None:
    """Return the most recent **passing** execution, or None if there is none.

    Each execution is a dict with at least `id`, `status`, and `end_time`. "Passing"
    is an explicit pass verdict (`PASSING_STATUSES`); everything else (failed,
    aborted, errored, still running, unset) is excluded. Most-recent is by
    `end_time`, with `id` as a deterministic tie-breaker. The input is not mutated.
    """
    passing = [e for e in executions if _is_passing(e)]
    if not passing:
        return None
    return max(passing, key=_sort_key)


def resolve_baseline(
    baseline: dict[str, str], test_id: str | int, executions: list[dict] | None = None
) -> dict:
    """Resolve the active baseline for a test: pinned id wins, else last passing run.

    Returns `{"source": "pinned"|"last-passing"|"none", "execution_id": <id or None>}`.
    A pinned entry in the committed file always takes precedence; otherwise fall back
    to the last passing run from `executions` (if provided). This mirrors ADR-0017's
    selection rule.
    """
    test_id = str(test_id).strip()
    pinned = baseline.get(test_id)
    if pinned is not None:
        return {"source": "pinned", "execution_id": pinned}
    if executions:
        chosen = select_last_passing(executions)
        if chosen is not None:
            return {"source": "last-passing", "execution_id": str(chosen.get("id"))}
    return {"source": "none", "execution_id": None}


# --- CLI ---------------------------------------------------------------------


def _load_executions_arg(path: str) -> list[dict]:
    raw = Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict) and "executions" in data:
        data = data["executions"]
    if not isinstance(data, list):
        raise BaselineError("--executions file must be a JSON list of execution objects")
    return data


def _cmd_resolve(args: argparse.Namespace) -> int:
    baseline = load_baseline(args.file)
    executions = _load_executions_arg(args.executions) if args.executions else None
    result = resolve_baseline(baseline, args.test_id, executions)
    print(json.dumps(result, indent=2))
    return 0


def _cmd_last_passing(args: argparse.Namespace) -> int:
    executions = _load_executions_arg(args.executions)
    chosen = select_last_passing(executions)
    print(json.dumps(chosen, indent=2) if chosen is not None else "null")
    return 0


def _cmd_set(args: argparse.Namespace) -> int:
    old = load_baseline(args.file)
    new = merge_baseline(old, args.test_id, args.execution_id)
    diff = diff_baseline(old, new, path=str(args.file))
    if diff:
        print(diff, end="")
    else:
        print("(no change — baseline already points at that execution)")
    if args.write:
        write_baseline(args.file, new)
        print("\nwrote %s" % args.file)
    else:
        print("\n(dry run — pass --write to apply)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pure baseline logic for the bzm-set-baseline skill "
        "(CI baseline file read/merge/write + last-passing selection).",
    )
    sub = parser.add_subparsers(dest="command")

    p_resolve = sub.add_parser(
        "resolve", help="Resolve the active baseline (pinned id, else last passing run)."
    )
    p_resolve.add_argument("--file", default=".blazemeter/baseline.json", help="baseline file path")
    p_resolve.add_argument("--test-id", required=True, help="test id to resolve")
    p_resolve.add_argument("--executions", help="JSON file of executions for last-passing fallback")
    p_resolve.set_defaults(func=_cmd_resolve)

    p_lp = sub.add_parser(
        "last-passing", help="Pick the most recent passing execution from a list."
    )
    p_lp.add_argument("--executions", required=True, help="JSON file of execution objects")
    p_lp.set_defaults(func=_cmd_last_passing)

    p_set = sub.add_parser(
        "set", help="Pin test_id -> execution_id in the baseline file (shows a diff)."
    )
    p_set.add_argument("--file", default=".blazemeter/baseline.json", help="baseline file path")
    p_set.add_argument("--test-id", required=True, help="test id to pin")
    p_set.add_argument("--execution-id", required=True, help="execution id to pin it to")
    p_set.add_argument("--write", action="store_true", help="apply the change (default: dry run)")
    p_set.set_defaults(func=_cmd_set)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except BaselineError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
