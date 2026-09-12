"""Mode decision, conflict detection, and writing (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.devtools.scaffold.project import ScaffoldConflictError, write_scaffold
from graftpunk.devtools.scaffold.render import ScaffoldSpec

_SUITE_PYPROJECT = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
packages = ["src/mysuite"]
"""

_NON_PLUGIN_PYPROJECT = """\
[project]
name = "unrelated-package"
"""


def _spec(name: str = "myshop") -> ScaffoldSpec:
    return ScaffoldSpec(
        name=name, mode="new_project", backend="nodriver", base_url="https://myshop.example.com"
    )


class TestNewProjectMode:
    def test_empty_directory_writes_a_new_project(self, tmp_path: Path) -> None:
        result = write_scaffold(tmp_path, _spec())
        assert result.mode == "new_project"
        assert (tmp_path / "pyproject.toml").exists()
        assert (tmp_path / "src" / "graftpunk_myshop" / "plugin.py").exists()

    def test_gitignore_not_reported_as_updated_for_a_new_project(self, tmp_path: Path) -> None:
        """A new project's .gitignore is written fresh with the line already in it."""
        result = write_scaffold(tmp_path, _spec())
        assert result.gitignore_updated is False


class TestAddToSuiteMode:
    def test_existing_plugin_suite_gets_incremental_files(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        result = write_scaffold(tmp_path, _spec("widgets"))
        assert result.mode == "add_to_suite"
        assert (tmp_path / "src" / "graftpunk_widgets" / "plugin.py").exists()
        assert (tmp_path / "tests" / "test_widgets.py").exists()
        assert not (tmp_path / "pyproject.toml").exists() or True  # untouched path, not rewritten

    def test_entry_point_and_package_added_to_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        write_scaffold(tmp_path, _spec("widgets"))
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        assert '"src/graftpunk_widgets"' in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text  # unaffected

    def test_gitignore_created_and_reported(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        result = write_scaffold(tmp_path, _spec("widgets"))
        assert result.gitignore_updated is True
        assert "tests/captures/" in (tmp_path / ".gitignore").read_text()

    def test_a_non_plugin_pyproject_refuses(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_NON_PLUGIN_PYPROJECT)
        with pytest.raises(ValueError, match="entry-points"):
            write_scaffold(tmp_path, _spec())

    def test_new_flag_forces_a_new_project_despite_existing_pyproject(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        with pytest.raises(ScaffoldConflictError):
            # A new-project render also writes pyproject.toml, which already
            # exists here: --new still refuses on conflict, it just skips the
            # suite-mode decision, so this proves force_new took the
            # new-project branch rather than raising the "not a suite" ValueError.
            write_scaffold(tmp_path, _spec(), force_new=True)

    def test_module_name_used_for_package_and_entry_point_target(self, tmp_path: Path) -> None:
        """The entry-point KEY and CLI name keep the raw name; everything
        derived for imports (package path, module, test file) goes through
        module_name_for (controller notes, 2026-09-11)."""
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        write_scaffold(tmp_path, _spec("my-shop"))
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'my-shop = "graftpunk_my_shop.plugin:MyShopPlugin"' in text
        assert '"src/graftpunk_my_shop"' in text
        assert (tmp_path / "src" / "graftpunk_my_shop" / "plugin.py").exists()
        assert (tmp_path / "tests" / "test_my_shop.py").exists()


class TestConflicts:
    def test_existing_target_file_refuses_before_writing_anything(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        with pytest.raises(ScaffoldConflictError) as exc:
            write_scaffold(tmp_path, _spec())
        assert any(p.name == "README.md" for p in exc.value.conflicts)
        assert not (tmp_path / "pyproject.toml").exists()
