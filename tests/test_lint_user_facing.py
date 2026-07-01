"""Tests for the user-facing-surface linter.

Skills, commands, and runtime assets ship into end users' Claude sessions
(conventions §9), so contributor-doc references must not appear in them. Tests
assert *external behaviour*: given text, which lint errors come back.
"""

from pathlib import Path

import lint_user_facing as luf

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- clean text passes --------------------------------------------------------

CLEAN = """\
---
name: bzm-example
description: Does a thing. Use when asked to do the thing.
---

Always resolve and **display** the full context (with ids) before acting.
Run `python ${CLAUDE_PLUGIN_ROOT}/shared/scripts/bzm_baseline.py --help`.
Reuse the shared script from `bzm-baseline`; read `.blazemeter/baseline.json`.
"""


def test_clean_skill_text_has_no_errors():
    assert luf.lint_text(CLEAN) == []


def test_user_repo_conventions_wording_is_allowed():
    # Ordinary English use of the word "conventions" is fine; only doc
    # references and section citations are banned.
    assert luf.lint_text("Follow your repo's naming conventions for the file.") == []


# --- each forbidden reference is caught ----------------------------------------


def _flags(text):
    return luf.lint_text(text)


def test_conventions_doc_reference_is_caught():
    assert _flags("This is the canonical step from `shared/conventions.md` §4.")
    assert _flags("see conventions.md for details")


def test_conventions_section_citation_is_caught():
    assert _flags("MCP-first per conventions §5.")


def test_adr_reference_is_caught():
    assert _flags("Two representations (ADR-0017), kept separate.")
    assert _flags("see docs/adr/0017-performance-baseline.md")


def test_section_sign_is_caught():
    assert _flags("Apply the tiered pick rule (§4.2) at each level.")


def test_issue_and_prd_references_are_caught():
    assert _flags("Per the PRD (issue #1), commands are thin entry points.")
    assert _flags("tracked in issue #42")


def test_contributor_doc_names_are_caught():
    assert _flags("see CLAUDE.md for the dev loop")
    assert _flags("domain language lives in CONTEXT.md")
    assert _flags("labels are described in docs/agents/triage-labels.md")
    assert _flags("this is required by the Definition of Done")


def test_errors_carry_line_numbers():
    errors = luf.lint_text("clean line\nsee ADR-0016 for why\n")
    assert errors and errors[0].startswith("line 2:")


# --- file-level API -------------------------------------------------------------


def test_lint_file_reads_from_disk(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("References conventions §4.\n")
    assert luf.lint_file(f) != []


# --- the shipped surfaces must pass their own linter ----------------------------


def test_shipped_skills_and_commands_are_free_of_contributor_references():
    files = luf._iter_files([str(REPO_ROOT / "skills"), str(REPO_ROOT / "commands")])
    assert files, "expected lintable files under skills/ and commands/"
    problems = {}
    for f in files:
        errors = luf.lint_file(f)
        if errors:
            problems[str(f.relative_to(REPO_ROOT))] = errors
    assert problems == {}, problems
