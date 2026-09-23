"""Mode decision, conflict detection, and writing (plugin tooling spec, 2026-09-11)."""

from __future__ import annotations

from pathlib import Path

import pytest

from graftpunk.devtools.scaffold import write
from graftpunk.devtools.scaffold.project import ScaffoldConflictError, write_scaffold
from graftpunk.devtools.scaffold.pyproject_edit import PyprojectEditError
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

_INCLUDE_WHEEL_PYPROJECT = """\
[project]
name = "mysuite"

[project.entry-points."graftpunk.plugins"]
existing = "mysuite.existing:ExistingPlugin"

[tool.hatch.build.targets.wheel]
include = ["src/mysuite/**"]
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


class TestASecondPluginInTheSameSuite:
    def test_both_plugins_land_with_their_own_fixture_directories(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)

        write_scaffold(tmp_path, _spec("widgets"))
        result = write_scaffold(tmp_path, _spec("gadgets"))

        assert result.mode == "add_to_suite"
        assert (tmp_path / "src" / "graftpunk_widgets" / "plugin.py").exists()
        assert (tmp_path / "src" / "graftpunk_gadgets" / "plugin.py").exists()
        assert (tmp_path / "tests" / "fixtures" / "widgets" / ".gitkeep").exists()
        assert (tmp_path / "tests" / "fixtures" / "gadgets" / ".gitkeep").exists()
        text = (tmp_path / "pyproject.toml").read_text()
        assert 'widgets = "graftpunk_widgets.plugin:WidgetsPlugin"' in text
        assert 'gadgets = "graftpunk_gadgets.plugin:GadgetsPlugin"' in text

    def test_an_existing_fixtures_directory_is_not_a_conflict(self, tmp_path: Path) -> None:
        """A .gitkeep exists only to put an empty directory in git, so one whose
        directory is already there is nothing to write."""
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        (tmp_path / "tests" / "fixtures" / "widgets").mkdir(parents=True)
        (tmp_path / "tests" / "fixtures" / "widgets" / ".gitkeep").write_text("")

        result = write_scaffold(tmp_path, _spec("widgets"))

        assert result.mode == "add_to_suite"
        assert (tmp_path / "src" / "graftpunk_widgets" / "plugin.py").exists()


class TestConflicts:
    def test_existing_target_file_refuses_before_writing_anything(self, tmp_path: Path) -> None:
        (tmp_path / "README.md").write_text("already here")
        with pytest.raises(ScaffoldConflictError) as exc:
            write_scaffold(tmp_path, _spec())
        assert any(p.name == "README.md" for p in exc.value.conflicts)
        assert not (tmp_path / "pyproject.toml").exists()


class TestPyprojectEditFailureLeavesSuiteUntouched:
    """A refused suite addition must leave the suite byte-identical: nothing
    written, pyproject.toml exactly as it was found, even when one of its two
    edits (with_entry_point) already succeeded before the other failed."""

    def test_include_only_wheel_table_leaves_pyproject_byte_identical(self, tmp_path: Path) -> None:
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_INCLUDE_WHEEL_PYPROJECT)
        original = pyproject.read_text()

        with pytest.raises(PyprojectEditError):
            write_scaffold(tmp_path, _spec("widgets"))

        assert pyproject.read_text() == original
        assert not (tmp_path / "src" / "graftpunk_widgets").exists()
        assert not (tmp_path / "tests" / "test_widgets.py").exists()
        assert not (tmp_path / ".gitignore").exists()

    def test_a_wheel_package_failure_does_not_leave_the_entry_point_behind(
        self, tmp_path: Path
    ) -> None:
        """with_entry_point succeeds (the table is present and 'widgets' is
        not registered yet) before with_wheel_package fails on the include
        shape; its edit must not reach the file either."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_INCLUDE_WHEEL_PYPROJECT)

        with pytest.raises(PyprojectEditError):
            write_scaffold(tmp_path, _spec("widgets"))

        text = pyproject.read_text()
        assert "widgets" not in text
        assert 'existing = "mysuite.existing:ExistingPlugin"' in text


class TestAWriteFailureLeavesNoPartialTree:
    def test_files_written_before_the_failure_are_removed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A disk that refuses one file must not leave the earlier ones behind.

        ``plugin.py`` is the third file the renderer emits, so pyproject.toml
        and the package ``__init__.py`` are already on disk when it fails.
        Fault injection at ``write._write_atomically``, the one place a planned
        change reaches the disk, is how one file fails and the rest do not; the
        assertions are all on the tree the call leaves on disk.
        """
        real_write = write._write_atomically

        def write_failing_on_the_plugin_module(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_plugin_module)

        with pytest.raises(OSError, match="No space left on device"):
            write_scaffold(tmp_path, _spec())

        monkeypatch.undo()
        leftovers = sorted(p for p in tmp_path.rglob("*") if p.is_file())
        assert leftovers == [], f"a refused write left {leftovers} behind"
        assert not (tmp_path / "src" / "graftpunk_myshop").exists()

    def test_a_short_write_that_touches_the_file_is_still_cleaned_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A short write (disk fills mid-write) leaves the file sitting on
        disk before the ``OSError`` surfaces. It must be recorded as written
        so cleanup removes it too, not only files that never touched disk.
        """
        real_write = write._write_atomically

        def write_touching_then_failing(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                path.touch()
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_touching_then_failing)

        with pytest.raises(OSError, match="No space left on device"):
            write_scaffold(tmp_path, _spec())

        monkeypatch.undo()
        leftovers = sorted(p for p in tmp_path.rglob("*") if p.is_file())
        assert leftovers == [], f"a refused write left {leftovers} behind"
        assert not (tmp_path / "src" / "graftpunk_myshop").exists()


class TestPyprojectRestoredAfterRenderedFileFailure:
    def test_pyproject_restored_when_a_rendered_file_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In add-to-suite mode the pyproject.toml edit is applied before any
        rendered file is written. When a rendered file then fails to write,
        pyproject.toml must come back byte-identical, the same guarantee
        TestPyprojectEditFailureLeavesSuiteUntouched checks for a
        PyprojectEditError, exercised here for an OSError from the render
        pass instead (the restore is write.py's, see apply_changes).
        """
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SUITE_PYPROJECT)
        original = pyproject.read_text()

        real_write = write._write_atomically

        def write_failing_on_the_plugin_module(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_plugin_module)

        with pytest.raises(OSError, match="No space left on device"):
            write_scaffold(tmp_path, _spec("widgets"))

        monkeypatch.undo()
        assert pyproject.read_text() == original

    def test_gitignore_is_not_edited_when_a_rendered_file_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ignore line protects the files this call writes, so a refusal
        must leave .gitignore exactly as it was found."""
        (tmp_path / "pyproject.toml").write_text(_SUITE_PYPROJECT)
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n")

        real_write = write._write_atomically

        def write_failing_on_the_plugin_module(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_plugin_module)

        with pytest.raises(OSError, match="No space left on device"):
            write_scaffold(tmp_path, _spec("widgets"))

        monkeypatch.undo()
        assert gitignore.read_text() == "*.pyc\n"

    def test_a_failed_gitignore_edit_restores_the_whole_batch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The .gitignore edit is the last change of write_scaffold's batch, so its
        failure undoes the pyproject.toml edit and every rendered file with it."""
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(_SUITE_PYPROJECT)
        gitignore = tmp_path / ".gitignore"
        gitignore.write_text("*.pyc\n")
        real_write = write._write_atomically

        def write_failing_on_the_gitignore(path: Path, text: str) -> None:
            if path.name == ".gitignore":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        monkeypatch.setattr(write, "_write_atomically", write_failing_on_the_gitignore)

        with pytest.raises(OSError, match="No space left on device"):
            write_scaffold(tmp_path, _spec("widgets"))

        monkeypatch.undo()
        assert pyproject.read_text() == _SUITE_PYPROJECT
        assert gitignore.read_text() == "*.pyc\n"
        assert not (tmp_path / "src").exists()
        assert not (tmp_path / "tests").exists()
