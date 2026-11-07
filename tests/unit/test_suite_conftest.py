"""A suite's shared conftest, edited by ``gp plugin new`` and ``gp plugin upgrade``
alike (#370): only what gp wrote changes."""

from __future__ import annotations

import ast

from graftpunk.devtools.scaffold.suite_conftest import (
    has_check,
    read_suite_conftest,
    with_suite_check,
)

_STANDALONE = """\
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber

scrub_site_env = site_env_scrubber("OLDSHOP_")
FIXTURES_TREE = Path(__file__).parent / "fixtures"
sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE)
"""
_SCOPED = """\
from pathlib import Path

from graftpunk.testing.plugin import fixtures_are_sanitised, site_env_scrubber

scrub_site_env = site_env_scrubber("OLDSHOP_")
FIXTURES_TREE = Path(__file__).parent / "fixtures"
SUITE_MEMBER_FIXTURES = ("myshop",)
sanitised_fixtures = fixtures_are_sanitised(FIXTURES_TREE, skip=SUITE_MEMBER_FIXTURES)
"""


def test_the_standalone_check_is_scoped_in_place() -> None:
    text, changes = with_suite_check(_STANDALONE, "oldshop", ["myshop"])
    assert text == _SCOPED
    assert changes == (
        "added SUITE_MEMBER_FIXTURES",
        "sanitised_fixtures: skips SUITE_MEMBER_FIXTURES",
    )


def test_a_new_member_joins_the_tuple_and_nothing_else_changes() -> None:
    text, changes = with_suite_check(_SCOPED, "oldshop", ["myshop", "midshop"])
    assert text == _SCOPED.replace('("myshop",)', '("myshop", "midshop")')
    assert changes == ("SUITE_MEMBER_FIXTURES: added midshop",)
    assert with_suite_check(text, "oldshop", ["myshop", "midshop"]) == (text, ())


def test_a_conftest_without_the_check_gets_all_three_bindings() -> None:
    older = (
        "from graftpunk.testing.plugin import site_env_scrubber\n\n"
        'scrub_site_env = site_env_scrubber("OLDSHOP_")\n'
    )
    text, changes = with_suite_check(older, "oldshop", ["myshop"])
    assert text == _SCOPED
    assert changes == (
        "added FIXTURES_TREE",
        "added SUITE_MEMBER_FIXTURES",
        "added sanitised_fixtures",
    )


def test_a_check_the_author_wrote_is_left_alone() -> None:
    custom = _STANDALONE.replace(
        "fixtures_are_sanitised(FIXTURES_TREE)",
        'fixtures_are_sanitised(FIXTURES_TREE, observe_dir="runs")',
    )
    assert with_suite_check(custom, "oldshop", ["myshop"]) == (custom, ())
    state = read_suite_conftest(ast.parse(custom))
    assert (state.unscoped, state.needs_skip, state.listed) == (False, False, None)


def test_a_check_that_reads_a_missing_tuple_gets_it_first() -> None:
    lacking = _SCOPED.replace('SUITE_MEMBER_FIXTURES = ("myshop",)\n', "")
    text, changes = with_suite_check(lacking, "oldshop", ["myshop"])
    assert text == _SCOPED
    assert changes == ("added SUITE_MEMBER_FIXTURES",)


def test_a_tuple_the_author_computes_is_left_alone() -> None:
    computed = _SCOPED.replace('("myshop",)', 'tuple(sorted(["myshop"]))')
    assert with_suite_check(computed, "oldshop", ["myshop", "midshop"]) == (computed, ())
    assert read_suite_conftest(ast.parse(computed)).listed is None


def test_crlf_line_endings_are_kept() -> None:
    text, _changes = with_suite_check(_STANDALONE.replace("\n", "\r\n"), "oldshop", ["myshop"])
    assert text == _SCOPED.replace("\n", "\r\n")


def test_without_add_check_a_conftest_with_no_check_is_left_alone() -> None:
    """Adding or upgrading a member never turns on the first plugin's check (#370)."""
    older = "from graftpunk.testing.plugin import site_env_scrubber\n"
    assert with_suite_check(older, "oldshop", ["myshop"], add_check=False) == (older, ())
    assert not has_check(older)


def test_without_add_check_gps_whole_tree_check_is_still_scoped() -> None:
    text, _changes = with_suite_check(_STANDALONE, "oldshop", ["myshop"], add_check=False)
    assert text == _SCOPED
    assert has_check(text)


def test_a_tuple_holding_an_entry_skip_refuses_is_never_appended_to() -> None:
    refused = _SCOPED.replace('("myshop",)', '("myshop/x", "", 3)')
    assert with_suite_check(refused, "oldshop", ["myshop"]) == (refused, ())
    state = read_suite_conftest(ast.parse(refused))
    assert (state.listed, state.refused) == (None, ("'myshop/x'", "''", "3"))
