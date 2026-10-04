"""The one seam that edits an existing pyproject.toml's text (plugin tooling spec,
2026-09-11). The functions are pure; the file on disk is write_scaffold's, and
tests/unit/test_scaffold_project.py covers it (TestAddToSuiteMode,
TestPyprojectEditFailureLeavesSuiteUntouched)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pyproject_edit import (
    CannotRaiseFloor,
    DynamicDependencies,
    PyprojectEditError,
    RaisedFloor,
    with_entry_point,
    with_graftpunk_floor,
    with_wheel_package,
)

_SINGLE_LINE_ARRAY = """\
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "mysuite"
version = "0.1.0"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/mysuite"]
"""

_MULTI_LINE_ARRAY = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = [
    "src/mysuite",
]
"""

_NO_ENTRY_POINT_TABLE = """\
[project]
name = "mysuite"
"""

_EMPTY_ENTRY_POINT_TABLE = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]

[tool.hatch.build.targets.wheel]
packages = ["src/mysuite"]
"""

_INCLUDE_INSTEAD_OF_PACKAGES = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]

[tool.hatch.build.targets.wheel]
include = ["src/mysuite/**"]
"""

_NO_PACKAGES_KEY_AT_ALL = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]

[tool.hatch.build.targets.wheel]
"""


_ARRAY_BEFORE_PACKAGES = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
exclude = ["docs"]
packages = ["src/mysuite"]
"""

_ENTRY_POINT_TABLE_LAST_NO_TRAILING_NEWLINE = (
    '[project]\nname = "mysuite"\n\n'
    '[project.entry-points."graftpunk.plugins"]\n'
    'existing = "mysuite.existing:ExistingPlugin"'
)

_PATH = Path("pyproject.toml")
_WIDGETS = "graftpunk_widgets.plugin:WidgetsPlugin"
_GADGETS = "graftpunk_gadgets.plugin:GadgetsPlugin"


def _entry_points(text: str) -> dict[str, str]:
    return tomllib.loads(text)["project"]["entry-points"]["graftpunk.plugins"]


def _wheel(text: str) -> dict[str, object]:
    return tomllib.loads(text)["tool"]["hatch"]["build"]["targets"]["wheel"]


class TestWithEntryPoint:
    def test_appends_a_new_line_to_the_table(self) -> None:
        text = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS)
        assert f'widgets = "{_WIDGETS}"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text

    def test_result_is_valid_toml(self) -> None:
        eps = _entry_points(with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS))
        assert eps["widgets"] == _WIDGETS
        assert eps["existing"] == "mysuite.existing:ExistingPlugin"

    def test_duplicate_name_refuses(self) -> None:
        with pytest.raises(PyprojectEditError, match="already registered"):
            with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "existing", _WIDGETS)

    def test_missing_table_refuses_with_instructions(self) -> None:
        with pytest.raises(PyprojectEditError, match="entry-points"):
            with_entry_point(_NO_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS)

    def test_appends_to_an_empty_table(self) -> None:
        text = with_entry_point(_EMPTY_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS)
        assert f'widgets = "{_WIDGETS}"' in text
        assert _entry_points(text)["widgets"] == _WIDGETS

    def test_the_blank_line_before_the_next_table_survives(self) -> None:
        lines = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS).splitlines()
        header_index = lines.index("[tool.hatch.build.targets.wheel]")
        assert lines[header_index - 1] == ""
        assert lines[header_index - 2] == f'widgets = "{_WIDGETS}"'

    def test_two_adds_leave_both_entries_in_the_same_table(self) -> None:
        once = with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS)
        text = with_entry_point(once, _PATH, "gadgets", _GADGETS)
        assert _entry_points(text) == {
            "existing": "mysuite.existing:ExistingPlugin",
            "widgets": _WIDGETS,
            "gadgets": _GADGETS,
        }
        lines = text.splitlines()
        assert lines[lines.index("[tool.hatch.build.targets.wheel]") - 1] == ""
        assert _wheel(text)["packages"] == ["src/mysuite"]

    def test_an_empty_table_keeps_its_separator_too(self) -> None:
        lines = with_entry_point(_EMPTY_ENTRY_POINT_TABLE, _PATH, "widgets", _WIDGETS).splitlines()
        header_index = lines.index("[tool.hatch.build.targets.wheel]")
        assert lines[header_index - 1] == ""
        assert lines[header_index - 2] == f'widgets = "{_WIDGETS}"'

    def test_a_table_that_is_last_and_has_no_trailing_newline(self) -> None:
        """The table's pattern needs its last line terminated, so this shape was
        refused as unlocatable."""
        text = with_entry_point(
            _ENTRY_POINT_TABLE_LAST_NO_TRAILING_NEWLINE, _PATH, "widgets", _WIDGETS
        )
        assert text.endswith("\n")
        assert _entry_points(text) == {
            "existing": "mysuite.existing:ExistingPlugin",
            "widgets": _WIDGETS,
        }

    def test_the_refusal_names_the_path_it_was_given(self) -> None:
        with pytest.raises(PyprojectEditError, match="custom/pyproject.toml"):
            with_entry_point(_NO_ENTRY_POINT_TABLE, Path("custom/pyproject.toml"), "w", _WIDGETS)


class TestWithWheelPackage:
    def test_an_array_before_packages_is_still_located(self) -> None:
        """The span between the header and the packages key stopped at any "[",
        so an earlier array's own bracket made a known shape unlocatable."""
        wheel = _wheel(with_wheel_package(_ARRAY_BEFORE_PACKAGES, _PATH, "src/graftpunk_widgets"))
        assert set(wheel["packages"]) == {"src/mysuite", "src/graftpunk_widgets"}
        assert wheel["exclude"] == ["docs"]

    def test_appends_to_a_single_line_array(self) -> None:
        text = with_wheel_package(_SINGLE_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in text

    def test_appends_to_a_multi_line_array(self) -> None:
        text = with_wheel_package(_MULTI_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in text
        assert '"src/mysuite"' in text

    def test_result_is_valid_toml_with_both_packages(self) -> None:
        text = with_wheel_package(_MULTI_LINE_ARRAY, _PATH, "src/graftpunk_widgets")
        assert set(_wheel(text)["packages"]) == {"src/mysuite", "src/graftpunk_widgets"}

    @pytest.mark.parametrize(
        ("text", "package"),
        [
            (_SINGLE_LINE_ARRAY, "src/mysuite"),
            (_SINGLE_LINE_ARRAY.rstrip("\n"), "src/mysuite"),
            (_NO_PACKAGES_KEY_AT_ALL, "src/graftpunk_widgets"),
            (_NO_PACKAGES_KEY_AT_ALL.rstrip("\n"), "src/graftpunk_widgets"),
        ],
    )
    def test_a_no_op_returns_the_input_byte_for_byte(self, text: str, package: str) -> None:
        """Nothing is rewritten when nothing changes, not even a missing final newline."""
        assert with_wheel_package(text, _PATH, package) == text

    def test_include_instead_of_packages_refuses(self) -> None:
        with pytest.raises(PyprojectEditError, match="include"):
            with_wheel_package(_INCLUDE_INSTEAD_OF_PACKAGES, _PATH, "src/graftpunk_widgets")


class TestCrlfText:
    def test_both_edits_keep_crlf_and_make_the_same_change(self) -> None:
        """A CRLF file used to be read with its line endings translated, and so was
        rewritten LF throughout. The edit now keeps the file's own line endings."""
        crlf = _SINGLE_LINE_ARRAY.replace("\n", "\r\n")
        text = with_wheel_package(
            with_entry_point(crlf, _PATH, "widgets", _WIDGETS), _PATH, "src/graftpunk_widgets"
        )
        lf = with_wheel_package(
            with_entry_point(_SINGLE_LINE_ARRAY, _PATH, "widgets", _WIDGETS),
            _PATH,
            "src/graftpunk_widgets",
        )
        assert text.count("\n") == text.count("\r\n")
        assert text == lf.replace("\n", "\r\n")

    def test_mixed_line_endings_are_refused_by_both_edits(self) -> None:
        """Byte-exact or refuse: an LF table header among CRLF lines lost a \\r and
        misplaced the new entry, so a file that is neither all-LF nor all-CRLF is
        refused with a way out."""
        mixed = _SINGLE_LINE_ARRAY.replace("\n", "\r\n").replace(
            '[project.entry-points."graftpunk.plugins"]\r\n',
            '[project.entry-points."graftpunk.plugins"]\n',
        )
        with pytest.raises(PyprojectEditError, match="line endings"):
            with_entry_point(mixed, _PATH, "widgets", _WIDGETS)
        with pytest.raises(PyprojectEditError, match="line endings"):
            with_wheel_package(mixed, _PATH, "src/graftpunk_widgets")


def _deps(*lines: str) -> str:
    body = "".join(f"    {line}\n" for line in lines)
    return (
        "[project]\n"
        'name = "graftpunk-myshop"\n'
        "dependencies = [\n"
        f"{body}"
        "]\n"
        "\n"
        "[project.optional-dependencies]\n"
        'dev = ["graftpunk>=1.0", "pytest>=8.0.0"]\n'
    )


class TestWithGraftpunkFloor:
    """A write that adds code needing this graftpunk also makes the project
    declare it: a plain lower bound below the floor is raised in place, one at
    or above it is left alone, and any other form is reported, never guessed at."""

    def test_a_lower_bound_below_the_floor_is_raised_in_place(self) -> None:
        text = _deps('"httpx>=0.27",', '"graftpunk>=1.0",  # the plugin API')
        result = with_graftpunk_floor(text, "1.17.0")
        assert result == RaisedFloor(
            text=text.replace('"graftpunk>=1.0"', '"graftpunk>=1.17.0"', 1), previous="1.0"
        )
        assert 'dev = ["graftpunk>=1.0"' in result.text

    def test_extras_spacing_and_an_environment_marker_are_kept_byte_for_byte(self) -> None:
        literal = "'graftpunk[browser] >= 1.0 ; python_version >= \"3.11\"',"
        text = _deps(literal)
        result = with_graftpunk_floor(text, "1.17.0")
        assert isinstance(result, RaisedFloor)
        assert result.text == text.replace(">= 1.0 ;", ">= 1.17.0 ;")
        assert result.previous == "1.0"

    def test_a_basic_string_with_escaped_marker_quotes_is_raised(self) -> None:
        text = _deps('"graftpunk[browser]>=1.0; python_version >= \\"3.11\\"",')
        result = with_graftpunk_floor(text, "1.17.0")
        assert isinstance(result, RaisedFloor)
        assert result.text == text.replace("]>=1.0;", "]>=1.17.0;")
        assert tomllib.loads(result.text)["project"]["dependencies"] == [
            'graftpunk[browser]>=1.17.0; python_version >= "3.11"'
        ]

    def test_crlf_line_endings_are_kept(self) -> None:
        text = _deps('"graftpunk>=1.0",').replace("\n", "\r\n")
        result = with_graftpunk_floor(text, "1.17.0")
        assert isinstance(result, RaisedFloor)
        assert result.text == text.replace('>=1.0"', '>=1.17.0"', 1)

    def test_a_one_line_array_holding_a_bracket_inside_a_string_is_raised(self) -> None:
        text = (
            '[project]\nname = "x"\ndependencies = ["graftpunk[browser]>=1.0", "httpx"]  # see ]\n'
        )
        result = with_graftpunk_floor(text, "1.17.0")
        assert isinstance(result, RaisedFloor)
        assert result.text == text.replace(">=1.0", ">=1.17.0")

    @pytest.mark.parametrize("bound", ["1.17.0", "1.17", "1.18.2", "2.0"])
    def test_a_lower_bound_at_or_above_the_floor_is_nothing_to_do(self, bound: str) -> None:
        assert with_graftpunk_floor(_deps(f'"graftpunk>={bound}",'), "1.17.0") is None

    @pytest.mark.parametrize(
        "requirement",
        [
            "graftpunk>=1.17.0,<2",
            "graftpunk==1.17.0",
            "graftpunk==1.17.*",
            "graftpunk~=1.17.0",
            "graftpunk>1.17.0",
            "graftpunk>=2.0,<3",
        ],
    )
    def test_a_requirement_that_already_excludes_everything_below_the_floor_is_nothing_to_do(
        self, requirement: str
    ) -> None:
        """A bound, a pin, a compatible-release clause, or a cap, each already at
        or above the floor, needs no rewrite: raising it would change nothing a
        resolver sees (graft skill spec, amended 2026-10-04)."""
        text = _deps(f'"{requirement}",')
        assert with_graftpunk_floor(text, "1.17.0") is None

    def test_an_unparseable_specifier_version_cannot_be_raised(self) -> None:
        """``===`` permits any text as its "version"; one that packaging.version
        cannot parse is "cannot raise", not a crash."""
        text = _deps('"graftpunk===notaversion",')
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(
            requirement="graftpunk===notaversion"
        )

    @pytest.mark.parametrize(
        "requirement",
        [
            "graftpunk==1.0",
            "graftpunk~=1.0",
            "graftpunk<2",
            "graftpunk>=1.0,<2",
            "graftpunk",
            "graftpunk @ https://example.com/graftpunk-1.0-py3-none-any.whl",
            "graftpunk>1.0",
            "graftpunk>=",
        ],
    )
    def test_any_other_form_cannot_be_raised_and_is_named(self, requirement: str) -> None:
        text = _deps(f'"{requirement}",')
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(requirement=requirement)

    def test_no_graftpunk_requirement_cannot_be_raised(self) -> None:
        text = _deps('"graftpunk-extras>=1.0",')
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(requirement=None)

    def test_no_dependencies_key_cannot_be_raised(self) -> None:
        text = '[project]\nname = "x"\n'
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(requirement=None)

    def test_dependencies_listed_as_dynamic_is_its_own_verdict(self) -> None:
        """A build backend, not this array, supplies the project's
        dependencies; there is no literal here to raise, and the CLI's wording
        for "no graftpunk requirement" (add it to [project] dependencies)
        would ask for a key the metadata spec does not let sit beside its own
        name in dynamic."""
        text = '[project]\nname = "x"\ndynamic = ["dependencies"]\n'
        assert with_graftpunk_floor(text, "1.17.0") == DynamicDependencies()

    def test_dependencies_listed_as_dynamic_wins_even_if_also_present(self) -> None:
        text = _deps('"graftpunk>=1.0",').replace(
            "[project]", '[project]\ndynamic = ["dependencies"]'
        )
        assert with_graftpunk_floor(text, "1.17.0") == DynamicDependencies()

    def test_two_graftpunk_requirements_cannot_be_raised(self) -> None:
        text = _deps(
            "'graftpunk>=1.0; python_version < \"3.12\"',",
            "'graftpunk>=1.1; python_version >= \"3.12\"',",
        )
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(
            requirement=(
                'graftpunk>=1.0; python_version < "3.12" and '
                'graftpunk>=1.1; python_version >= "3.12"'
            )
        )

    def test_a_requirement_it_cannot_locate_textually_cannot_be_raised(self) -> None:
        text = '[project]\nname = "x"\ndependencies = [\n    """graftpunk>=1.0""",\n]\n'
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(
            requirement="graftpunk>=1.0"
        )

    def test_two_graftpunk_requirements_both_already_at_the_floor_are_nothing_to_do(
        self,
    ) -> None:
        """A marker-split pair each already excluding everything below the
        floor is nothing extra to do, the same as a single requirement in
        that shape."""
        text = _deps(
            "'graftpunk>=1.17.0; python_version < \"3.12\"',",
            "'graftpunk[browser]>=1.17.0; python_version >= \"3.12\"',",
        )
        assert with_graftpunk_floor(text, "1.17.0") is None

    def test_two_graftpunk_requirements_one_below_the_floor_cannot_be_raised(self) -> None:
        """Only one of the pair excludes everything below the floor: still
        cannot raise, and still named."""
        text = _deps(
            "'graftpunk>=1.0; python_version < \"3.12\"',",
            "'graftpunk[browser]>=1.17.0; python_version >= \"3.12\"',",
        )
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(
            requirement=(
                'graftpunk>=1.0; python_version < "3.12" and '
                'graftpunk[browser]>=1.17.0; python_version >= "3.12"'
            )
        )

    def test_two_graftpunk_requirements_one_unparseable_cannot_be_raised(self) -> None:
        """One literal of a marker-split pair passes _is_graftpunk's name
        match but is not a parseable Requirement: still cannot raise, named
        with the pair joined, not a crash."""
        text = _deps(
            '"graftpunk>=",',
            "'graftpunk>=1.17.0; python_version >= \"3.12\"',",
        )
        assert with_graftpunk_floor(text, "1.17.0") == CannotRaiseFloor(
            requirement='graftpunk>= and graftpunk>=1.17.0; python_version >= "3.12"'
        )
