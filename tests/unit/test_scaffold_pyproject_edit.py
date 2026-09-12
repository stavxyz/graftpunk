"""The one seam that edits an existing pyproject.toml (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.pyproject_edit import (
    PyprojectEditError,
    add_entry_point,
    add_wheel_package,
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


class TestAddEntryPoint:
    def test_appends_a_new_line_to_the_table(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")
        text = pyproject.read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text  # untouched

    def test_result_is_valid_toml(self, tmp_path: Path) -> None:
        import tomllib

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")
        data = tomllib.loads(pyproject.read_text())
        eps = data["project"]["entry-points"]["graftpunk.plugins"]
        assert eps["widgets"] == "graftpunk_widgets.plugin:WidgetsPlugin"
        assert eps["existing"] == "mysuite.existing:ExistingPlugin"

    def test_duplicate_name_refuses(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        with pytest.raises(PyprojectEditError, match="already registered"):
            add_entry_point(pyproject, "existing", "graftpunk_widgets.plugin:WidgetsPlugin")

    def test_missing_table_refuses_with_instructions(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_NO_ENTRY_POINT_TABLE)
        with pytest.raises(PyprojectEditError, match="entry-points"):
            add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")

    def test_appends_to_an_empty_table(self, tmp_path: Path) -> None:
        import tomllib

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_EMPTY_ENTRY_POINT_TABLE)
        add_entry_point(pyproject, "widgets", "graftpunk_widgets.plugin:WidgetsPlugin")
        text = pyproject.read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        data = tomllib.loads(text)
        eps = data["project"]["entry-points"]["graftpunk.plugins"]
        assert eps["widgets"] == "graftpunk_widgets.plugin:WidgetsPlugin"


class TestAddWheelPackage:
    def test_appends_to_a_single_line_array(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        assert '"src/graftpunk_widgets"' in pyproject.read_text()

    def test_appends_to_a_multi_line_array(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_MULTI_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        text = pyproject.read_text()
        assert '"src/graftpunk_widgets"' in text
        assert '"src/mysuite"' in text

    def test_result_is_valid_toml_with_both_packages(self, tmp_path: Path) -> None:
        import tomllib

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_MULTI_LINE_ARRAY)
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        packages = tomllib.loads(pyproject.read_text())["tool"]["hatch"]["build"]["targets"][
            "wheel"
        ]["packages"]
        assert set(packages) == {"src/mysuite", "src/graftpunk_widgets"}

    def test_already_present_is_a_no_op(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SINGLE_LINE_ARRAY)
        before = pyproject.read_text()
        add_wheel_package(pyproject, "src/mysuite")
        assert pyproject.read_text() == before

    def test_no_packages_key_is_a_no_op(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_NO_PACKAGES_KEY_AT_ALL)
        before = pyproject.read_text()
        add_wheel_package(pyproject, "src/graftpunk_widgets")
        assert pyproject.read_text() == before

    def test_include_instead_of_packages_refuses(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_INCLUDE_INSTEAD_OF_PACKAGES)
        with pytest.raises(PyprojectEditError, match="include"):
            add_wheel_package(pyproject, "src/graftpunk_widgets")
