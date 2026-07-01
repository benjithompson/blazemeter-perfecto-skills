#!/usr/bin/env python3
"""Lint user-facing plugin surfaces for contributor-doc references.

Skills, commands, and the assets they open at runtime are loaded into end users'
Claude sessions (see `shared/conventions.md` §9). Development-environment context
must never leak into them: no references to the conventions doc, ADRs, GitHub
issues/PRDs, CLAUDE.md/CONTEXT.md, agent docs, or section-sign citations. The rule
itself is inlined in the skill; its provenance stays in the dev docs.

The linter is intentionally dependency-free (Python standard library only) so the
CI lint step and `--help` smoke test need nothing installed. It exposes
`lint_text()` / `lint_file()` for tests and a CLI for CI.

Usage:
    python lint_user_facing.py skills commands   # lint the shipped surfaces
    python lint_user_facing.py path/to/SKILL.md  # lint specific files
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Each pattern is (compiled regex, human-readable reason). Patterns run per line.
FORBIDDEN = [
    (re.compile(r"conventions\.md", re.IGNORECASE), "references the contributor conventions doc"),
    (re.compile(r"shared/conventions", re.IGNORECASE), "references the contributor conventions doc"),
    (re.compile(r"\bconventions\s*§"), "cites the conventions doc by section"),
    (re.compile(r"\bADR-\d", re.IGNORECASE), "cites an ADR"),
    (re.compile(r"docs/adr", re.IGNORECASE), "references the ADR directory"),
    (re.compile(r"docs/agents", re.IGNORECASE), "references contributor agent docs"),
    (re.compile(r"§"), "uses a section-sign citation (inline the rule instead)"),
    (re.compile(r"\bissue\s+#\d", re.IGNORECASE), "references a tracker issue"),
    (re.compile(r"\bPRD\b"), "references the PRD"),
    (re.compile(r"\bCLAUDE\.md\b"), "references the contributor CLAUDE.md"),
    (re.compile(r"\bCONTEXT\.md\b"), "references the contributor CONTEXT.md"),
    (re.compile(r"Definition of Done", re.IGNORECASE), "references the contributor Definition of Done"),
]

# File types that are (or can be) read into a user's session from these surfaces.
LINTED_SUFFIXES = {".md", ".html", ".txt", ".json", ".yml", ".yaml"}


def lint_text(text: str) -> list[str]:
    """Lint text content. Returns error messages ([] == clean)."""
    errors: list[str] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern, reason in FORBIDDEN:
            match = pattern.search(line)
            if match:
                errors.append(
                    "line %d: %s (%r in %r)"
                    % (lineno, reason, match.group(0), line.strip()[:120])
                )
    return errors


def lint_file(path: str | Path) -> list[str]:
    """Lint one file; returns error messages ([] == clean)."""
    text = Path(path).read_text(encoding="utf-8")
    return lint_text(text)


def _iter_files(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files.extend(
                sorted(f for f in p.rglob("*") if f.is_file() and f.suffix in LINTED_SUFFIXES)
            )
        else:
            files.append(p)
    seen: set[Path] = set()
    unique: list[Path] = []
    for f in files:
        rp = f.resolve()
        if rp not in seen:
            seen.add(rp)
            unique.append(f)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Lint user-facing plugin surfaces (skills/, commands/) for "
            "contributor-doc references that must not ship to end users."
        ),
    )
    parser.add_argument(
        "paths",
        nargs="*",
        default=["skills", "commands"],
        help="Files or directories to lint (default: skills/ and commands/).",
    )
    args = parser.parse_args(argv)

    files = _iter_files(args.paths)
    if not files:
        print("no lintable files found in: %s" % ", ".join(args.paths), file=sys.stderr)
        return 1

    total_errors = 0
    for f in files:
        errors = lint_file(f)
        if errors:
            total_errors += len(errors)
            print("FAIL %s" % f)
            for e in errors:
                print("  - %s" % e)
        else:
            print("ok   %s" % f)

    if total_errors:
        print(
            "\n%d contributor-reference error(s) across %d file(s)" % (total_errors, len(files)),
            file=sys.stderr,
        )
        return 1
    print("\nAll %d user-facing file(s) are free of contributor references." % len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
