"""Fixture-driven tests for the SKILL.md frontmatter linter.

The linter is the one piece of deterministic logic in the plugin skeleton, so it
gets real tests (per the PRD testing decisions). Tests assert *external behaviour*:
given frontmatter text, which lint errors come back. New rules are added by adding a
case here first.
"""

from pathlib import Path

import pytest

import lint_frontmatter as lf

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


# --- the canonical good case -------------------------------------------------

GOOD = """\
---
name: analyze-blazemeter-test
description: Analyze a BlazeMeter test's execution history. Use when asked to trend or review a test.
---

Body goes here.
"""


def test_valid_frontmatter_has_no_errors():
    assert lf.lint_text(GOOD, expected_name="analyze-blazemeter-test") == []


def test_valid_frontmatter_ignores_unknown_optional_keys():
    text = GOOD.replace(
        "---\n\nBody",
        "allowed-tools: Bash, Read\ndisable-model-invocation: false\n---\n\nBody",
    )
    assert lf.lint_text(text, expected_name="analyze-blazemeter-test") == []


# --- the malformations the linter must catch ---------------------------------


def _has_error(errors, needle):
    return any(needle in e for e in errors), errors


def test_stray_backtick_before_fence_is_rejected():
    # The exact bug in the original analyze-blazemeter-test skill: a stray
    # backtick turns the opening `---` fence into `` `--- ``.
    text = "`" + GOOD
    errors = lf.lint_text(text, expected_name="analyze-blazemeter-test")
    ok, errs = _has_error(errors, "frontmatter")
    assert ok, errs


def test_leading_blank_line_before_fence_is_rejected():
    text = "\n" + GOOD
    assert lf.lint_text(text, expected_name="analyze-blazemeter-test") != []


def test_missing_opening_fence_is_rejected():
    text = "name: x\ndescription: y\n\nBody\n"
    assert lf.lint_text(text, expected_name="x") != []


def test_missing_closing_fence_is_rejected():
    text = "---\nname: x\ndescription: y\nBody with no closing fence\n"
    errors = lf.lint_text(text, expected_name="x")
    ok, errs = _has_error(errors, "closing")
    assert ok, errs


def test_missing_name_is_rejected():
    text = "---\ndescription: A description that is present.\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="x")
    ok, errs = _has_error(errors, "name")
    assert ok, errs


def test_missing_description_is_rejected():
    text = "---\nname: x\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="x")
    ok, errs = _has_error(errors, "description")
    assert ok, errs


def test_empty_description_is_rejected():
    text = '---\nname: x\ndescription: "   "\n---\nBody\n'
    errors = lf.lint_text(text, expected_name="x")
    ok, errs = _has_error(errors, "description")
    assert ok, errs


def test_non_kebab_case_name_is_rejected():
    text = "---\nname: Analyze_Test\ndescription: A valid description here.\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="Analyze_Test")
    ok, errs = _has_error(errors, "kebab")
    assert ok, errs


def test_name_must_match_directory():
    text = "---\nname: some-other-name\ndescription: A valid description here.\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="analyze-blazemeter-test")
    ok, errs = _has_error(errors, "director")
    assert ok, errs


def test_name_directory_match_skipped_when_no_expectation():
    # When linting raw text with no directory context, the dir-match rule is skipped.
    text = "---\nname: some-name\ndescription: A valid description here.\n---\nBody\n"
    assert lf.lint_text(text, expected_name=None) == []


def test_unparseable_frontmatter_line_is_rejected():
    text = "---\nname: x\nthis line has no colon\ndescription: A valid description.\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="x")
    assert errors != []


def test_duplicate_key_is_rejected():
    text = "---\nname: x\nname: y\ndescription: A valid description here.\n---\nBody\n"
    errors = lf.lint_text(text, expected_name="x")
    ok, errs = _has_error(errors, "duplicate")
    assert ok, errs


# --- file-level API and on-disk fixtures -------------------------------------


def test_lint_file_derives_expected_name_from_directory(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: my-skill\ndescription: Valid description text.\n---\nBody\n"
    )
    assert lf.lint_file(skill_dir / "SKILL.md") == []


def test_lint_file_flags_directory_mismatch(tmp_path):
    skill_dir = tmp_path / "my-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: wrong\ndescription: Valid description text.\n---\nBody\n"
    )
    assert lf.lint_file(skill_dir / "SKILL.md") != []


def test_malformed_on_disk_fixture_is_caught():
    # CI's "fails on a malformed one" guarantee, pinned to a real file on disk.
    bad = FIXTURES / "malformed-stray-backtick" / "SKILL.md"
    assert bad.exists(), "malformed fixture must exist for the CI-fails-on-bad test"
    assert lf.lint_file(bad) != []


# --- the real shipped skill must pass its own linter -------------------------


def test_shipped_skills_pass_the_linter():
    skill_files = sorted((REPO_ROOT / "skills").glob("*/SKILL.md"))
    assert skill_files, "expected at least one shipped skill under skills/"
    problems = {}
    for sf in skill_files:
        errors = lf.lint_file(sf)
        if errors:
            problems[str(sf.relative_to(REPO_ROOT))] = errors
    assert problems == {}, problems
