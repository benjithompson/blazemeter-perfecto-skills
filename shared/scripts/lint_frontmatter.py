#!/usr/bin/env python3
"""Lint the YAML frontmatter of skill `SKILL.md` files.

House style for this repo (see `shared/conventions.md`):

  * the file must open with a `---` fence on line 1 (no stray characters, BOM, or
    blank lines before it — that is exactly the bug that shipped in the original
    analyze-blazemeter-test skill);
  * the frontmatter is flat `key: value` lines, closed by a second `---` fence;
  * `name` and `description` are required; `name` is kebab-case and matches the
    skill's directory name.

The linter is intentionally dependency-free (Python standard library only) so the
CI lint step and `--help` smoke test need nothing installed. It exposes
`lint_text()` / `lint_file()` for tests and a CLI for CI.

Usage:
    python lint_frontmatter.py skills            # lint every skills/*/SKILL.md
    python lint_frontmatter.py path/to/SKILL.md  # lint specific files
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FENCE = "---"
REQUIRED_KEYS = ("name", "description")
KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_DESCRIPTION_LEN = 1024


def _split_lines(text: str) -> list[str]:
    # Splitlines handles \n, \r\n and \r uniformly without keeping the separators.
    return text.splitlines()


def extract_frontmatter(text: str) -> tuple[list[str] | None, str | None]:
    """Return (frontmatter_lines, error).

    Exactly one of the two is non-None. The opening fence must be the very first
    line so that a stray leading character (e.g. a backtick or BOM) or a blank
    line is reported rather than silently parsed.
    """
    lines = _split_lines(text)
    if not lines or lines[0] != FENCE:
        return None, (
            "missing opening frontmatter fence: the file must start with '---' on "
            "line 1 (found %r)" % (lines[0] if lines else "")
        )
    for idx in range(1, len(lines)):
        if lines[idx] == FENCE:
            return lines[1:idx], None
    return None, "missing closing frontmatter fence: no '---' after the opening fence"


def parse_frontmatter(block_lines: list[str]) -> tuple[dict[str, str], list[str]]:
    """Parse flat `key: value` frontmatter lines into a dict, collecting errors."""
    fields: dict[str, str] = {}
    errors: list[str] = []
    for raw in block_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            errors.append("unparseable frontmatter line (expected 'key: value'): %r" % raw)
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1]
        if not key:
            errors.append("frontmatter line is missing a key: %r" % raw)
            continue
        if key in fields:
            errors.append("duplicate frontmatter key: %r" % key)
            continue
        fields[key] = value
    return fields, errors


def validate_fields(fields: dict[str, str], expected_name: str | None) -> list[str]:
    """Apply the house-style rules to parsed frontmatter fields."""
    errors: list[str] = []

    for key in REQUIRED_KEYS:
        if key not in fields:
            errors.append("missing required frontmatter key: %r" % key)

    name = fields.get("name", "").strip()
    if "name" in fields:
        if not name:
            errors.append("frontmatter 'name' must not be empty")
        else:
            if not KEBAB_RE.match(name):
                errors.append("frontmatter 'name' must be kebab-case (got %r)" % name)
            if expected_name is not None and name != expected_name:
                errors.append(
                    "frontmatter 'name' %r must match its directory name %r"
                    % (name, expected_name)
                )

    if "description" in fields:
        description = fields.get("description", "").strip()
        if not description:
            errors.append("frontmatter 'description' must not be empty")
        elif len(description) > MAX_DESCRIPTION_LEN:
            errors.append(
                "frontmatter 'description' is too long (%d > %d chars)"
                % (len(description), MAX_DESCRIPTION_LEN)
            )

    return errors


def lint_text(text: str, *, expected_name: str | None = None) -> list[str]:
    """Lint frontmatter text. Returns a list of error messages ([] == valid)."""
    block, error = extract_frontmatter(text)
    if error is not None:
        return [error]
    fields, parse_errors = parse_frontmatter(block)
    return parse_errors + validate_fields(fields, expected_name)


def lint_file(path: str | Path) -> list[str]:
    """Lint a SKILL.md file; the expected name is its parent directory's name."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    return lint_text(text, expected_name=path.parent.name)


def _iter_skill_files(paths: list[str]) -> list[Path]:
    skill_files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            skill_files.extend(sorted(p.rglob("SKILL.md")))
        else:
            skill_files.append(p)
    # De-duplicate while preserving order.
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in skill_files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Lint the YAML frontmatter of skill SKILL.md files.",
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["skills"],
        help="SKILL.md files or directories to search (default: skills/).",
    )
    args = parser.parse_args(argv)

    skill_files = _iter_skill_files(args.paths)
    if not skill_files:
        print("no SKILL.md files found in: %s" % ", ".join(args.paths), file=sys.stderr)
        return 1

    total_errors = 0
    for sf in skill_files:
        errors = lint_file(sf)
        if errors:
            total_errors += len(errors)
            print("FAIL %s" % sf)
            for e in errors:
                print("  - %s" % e)
        else:
            print("ok   %s" % sf)

    if total_errors:
        print("\n%d frontmatter error(s) across %d file(s)" % (total_errors, len(skill_files)),
              file=sys.stderr)
        return 1
    print("\nAll %d SKILL.md frontmatter blocks are valid." % len(skill_files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
