"""The one seam that edits an existing pyproject.toml's text (plugin tooling spec,
2026-09-11). The functions are pure; the file on disk is write_scaffold's, and
tests/unit/test_scaffold_project.py covers it (TestAddToSuiteMode,
TestPyprojectEditFailureLeavesSuiteUntouched)."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    with_entry_point,
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
