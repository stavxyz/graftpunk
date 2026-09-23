"""The one write discipline for every scaffold mutator (graft skill spec, 2026-09-21,
"One write discipline for every mutator")."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

import pytest

import graftpunk.devtools.scaffold as scaffold_package
from graftpunk.devtools.errors import ScaffoldWriteError
from graftpunk.devtools.scaffold import write
from graftpunk.devtools.scaffold.write import (
    ChangeConflictError,
    InvalidChangeError,
    PlannedChange,
    apply_changes,
    validate_python,
    validate_toml,
)

_SCAFFOLD_DIR = Path(scaffold_package.__file__).parent


def _files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _project_root(tmp_path: Path) -> Path:
    """An empty directory of the test's own. tmp_path itself also holds the config
    directory the autouse isolated_config fixture creates, so a listing of it is
    never empty."""
    root = tmp_path / "project"
    root.mkdir()
    return root


class TestRefusesBeforeTouching:
    def test_a_create_over_an_existing_file_writes_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "taken.py").write_text("x = 1\n")
        before = _files(tmp_path)
        with pytest.raises(ChangeConflictError) as caught:
            apply_changes(
                [
                    PlannedChange(tmp_path / "new" / "a.py", "a = 1\n", validate=validate_python),
                    PlannedChange(tmp_path / "taken.py", "x = 2\n", validate=validate_python),
                ]
            )
        assert caught.value.conflicts == [tmp_path / "taken.py"]
        assert _files(tmp_path) == before
        assert not (tmp_path / "new").exists()

    def test_an_edit_whose_file_changed_since_it_was_planned_is_refused(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "plugin.py"
        target.write_text("x = 2\n")
        with pytest.raises(ChangeConflictError):
            apply_changes([PlannedChange(target, "x = 3\n", original="x = 1\n")])
        assert target.read_text() == "x = 2\n"

    def test_a_module_that_does_not_parse_is_refused_and_nothing_is_written(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(InvalidChangeError, match="does not parse"):
            apply_changes(
                [
                    PlannedChange(tmp_path / "ok.py", "ok = 1\n", validate=validate_python),
                    PlannedChange(tmp_path / "bad.py", "def (:\n", validate=validate_python),
                ]
            )
        assert _files(tmp_path) == {}

    def test_a_pyproject_that_does_not_load_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(InvalidChangeError, match="does not load"):
            apply_changes(
                [PlannedChange(tmp_path / "pyproject.toml", "[project\n", validate=validate_toml)]
            )
        assert _files(tmp_path) == {}


class TestTheValidatorIsTheChanges:
    def test_the_writer_runs_the_carried_validator_whatever_the_suffix(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(InvalidChangeError):
            apply_changes(
                [PlannedChange(tmp_path / "notes.txt", "def (:\n", validate=validate_python)]
            )

    def test_a_change_with_no_validator_is_written_as_given(self, tmp_path: Path) -> None:
        apply_changes([PlannedChange(tmp_path / "raw.py", "def (:\n")])
        assert (tmp_path / "raw.py").read_text() == "def (:\n"


class TestRestoresOnFailure:
    def test_an_edit_is_restored_and_a_create_removed_when_a_later_write_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        edited = tmp_path / "pyproject.toml"
        edited.write_text('[project]\nname = "x"\n')
        real = write._write_atomically

        def failing(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real(path, text)

        monkeypatch.setattr(write, "_write_atomically", failing)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(
                        edited,
                        '[project]\nname = "y"\n',
                        original='[project]\nname = "x"\n',
                        validate=validate_toml,
                    ),
                    PlannedChange(tmp_path / "src" / "a.py", "a = 1\n"),
                    PlannedChange(tmp_path / "src" / "plugin.py", "p = 1\n"),
                ]
            )
        assert _files(tmp_path) == {"pyproject.toml": b'[project]\nname = "x"\n'}
        assert not (tmp_path / "src").exists()
        message = str(caught.value)
        assert message.startswith(f"Could not write {tmp_path / 'src' / 'plugin.py'}: ")
        assert "No space left on device" in message
        assert "\n" not in message
        assert message.endswith("Every file this operation had changed was restored.")
        assert caught.value.path == tmp_path / "src" / "plugin.py"
        assert caught.value.unrestored == ()
        assert caught.value.error.errno == 28
        assert caught.value.errno == 28

    def test_a_path_the_restore_cannot_put_back_is_named(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The message promises a full restore only when there was one."""
        created = tmp_path / "src" / "a.py"
        real_write = write._write_atomically
        real_unlink = Path.unlink

        def failing_write(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real_write(path, text)

        def failing_unlink(self: Path, missing_ok: bool = False) -> None:
            if self == created:
                raise OSError(13, "Permission denied", str(self))
            real_unlink(self, missing_ok=missing_ok)

        monkeypatch.setattr(write, "_write_atomically", failing_write)
        monkeypatch.setattr(Path, "unlink", failing_unlink)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(created, "a = 1\n"),
                    PlannedChange(tmp_path / "src" / "plugin.py", "p = 1\n"),
                ]
            )
        assert created in caught.value.unrestored
        message = str(caught.value)
        assert str(created) in message
        assert "Every file this operation had changed was restored" not in message
        assert "\n" not in message

    def test_a_parent_that_is_a_file_restores_and_leaves_no_temp(self, tmp_path: Path) -> None:
        root = _project_root(tmp_path)
        (root / "blocker").write_text("a file where a directory should be")
        with pytest.raises(ScaffoldWriteError):
            apply_changes(
                [
                    PlannedChange(root / "first.py", "a = 1\n"),
                    PlannedChange(root / "blocker" / "second.py", "b = 1\n"),
                ]
            )
        assert sorted(p.name for p in root.iterdir()) == ["blocker"]

    def test_no_partial_file_is_left_when_the_replace_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def failing_replace(src: object, dst: object) -> None:
            raise OSError(5, "Input/output error")

        root = _project_root(tmp_path)
        monkeypatch.setattr(write.os, "replace", failing_replace)
        with pytest.raises(ScaffoldWriteError):
            apply_changes([PlannedChange(root / "a.py", "a = 1\n")])
        assert list(root.iterdir()) == []


class TestNothingButAnOSErrorSkipsTheRestore:
    def test_content_that_cannot_be_encoded_is_refused_before_anything_is_written(
        self, tmp_path: Path
    ) -> None:
        root = _project_root(tmp_path)
        edited = root / "pyproject.toml"
        edited.write_text('[project]\nname = "x"\n')
        with pytest.raises(InvalidChangeError) as caught:
            apply_changes(
                [
                    PlannedChange(
                        edited, '[project]\nname = "y"\n', original='[project]\nname = "x"\n'
                    ),
                    PlannedChange(root / "README.md", "lone surrogate \ud800\n"),
                ]
            )
        assert caught.value.path == root / "README.md"
        assert "UTF-8" in str(caught.value)
        assert _files(root) == {"pyproject.toml": b'[project]\nname = "x"\n'}

    @pytest.mark.parametrize("raised", [KeyboardInterrupt, RuntimeError])
    def test_a_non_os_error_mid_write_restores_and_is_re_raised_unwrapped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raised: type[BaseException]
    ) -> None:
        """The partial is on disk when the exception lands: it is removed, the earlier
        edit is put back, and the caller sees the exception itself."""
        root = _project_root(tmp_path)
        edited = root / "pyproject.toml"
        edited.write_text('[project]\nname = "x"\n')
        real_write_text = Path.write_text

        def interrupted_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
            if self.name.startswith(".plugin.py"):
                real_write_text(self, data[:3], *args, **kwargs)  # ty: ignore[invalid-argument-type]
                raise raised()
            return real_write_text(self, data, *args, **kwargs)  # ty: ignore[invalid-argument-type]

        monkeypatch.setattr(Path, "write_text", interrupted_write_text)
        with pytest.raises(raised) as caught:
            apply_changes(
                [
                    PlannedChange(
                        edited, '[project]\nname = "y"\n', original='[project]\nname = "x"\n'
                    ),
                    PlannedChange(root / "src" / "plugin.py", "p = 1\n"),
                ]
            )
        monkeypatch.undo()
        assert type(caught.value) is raised
        assert _files(root) == {"pyproject.toml": b'[project]\nname = "x"\n'}
        assert not (root / "src").exists()


class TestRestoresAreByteExact:
    _CRLF = '[project]\r\nname = "x"\r\n'

    def test_a_crlf_edit_is_restored_byte_for_byte(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = _project_root(tmp_path)
        edited = root / "pyproject.toml"
        edited.write_bytes(self._CRLF.encode())
        real = write._write_atomically

        def failing(path: Path, text: str) -> None:
            if path.name == "plugin.py":
                raise OSError(28, "No space left on device", str(path))
            real(path, text)

        monkeypatch.setattr(write, "_write_atomically", failing)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(edited, '[project]\r\nname = "y"\r\n', original=self._CRLF),
                    PlannedChange(root / "plugin.py", "p = 1\n"),
                ]
            )
        assert edited.read_bytes() == self._CRLF.encode()
        assert caught.value.unrestored == ()

    def test_a_crlf_original_read_as_bytes_is_not_a_conflict_and_is_written_as_given(
        self, tmp_path: Path
    ) -> None:
        target = tmp_path / "pyproject.toml"
        target.write_bytes(self._CRLF.encode())
        original = write.read_original(target)
        assert original == self._CRLF
        apply_changes([PlannedChange(target, original + "[tool]\r\n", original=original)])
        assert target.read_bytes() == (self._CRLF + "[tool]\r\n").encode()

    def test_an_original_that_is_not_utf8_is_a_named_refusal(self, tmp_path: Path) -> None:
        target = tmp_path / ".gitignore"
        target.write_bytes(b"caf\xe9\n")
        with pytest.raises(InvalidChangeError, match="not UTF-8 text") as caught:
            write.read_original(target)
        assert caught.value.path == target


class TestTheWriterKeepsWhatItDoesNotChange:
    def test_an_edit_whose_own_write_fails_is_left_intact_not_rewritten(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The atomic write left the file holding its original text, so the restore
        must not write it again: on the full disk that failed the edit, a rewrite
        could be what truncates it."""
        target = tmp_path / "pyproject.toml"
        target.write_text('[project]\nname = "x"\n')
        real_write_text = Path.write_text

        def full_disk_write_text(self: Path, *args: object, **kwargs: object) -> int:
            """A write on the full disk: the file is truncated, then the write fails."""
            if self.resolve().parent == tmp_path.resolve():
                real_write_text(self, "")
                raise OSError(28, "No space left on device", str(self))
            return real_write_text(self, *args, **kwargs)  # ty: ignore[invalid-argument-type]

        monkeypatch.setattr(Path, "write_text", full_disk_write_text)
        with pytest.raises(ScaffoldWriteError) as caught:
            apply_changes(
                [
                    PlannedChange(
                        target, '[project]\nname = "y"\n', original='[project]\nname = "x"\n'
                    )
                ]
            )
        monkeypatch.undo()
        assert target.read_text() == '[project]\nname = "x"\n'
        assert caught.value.unrestored == ()
        assert [p.name for p in tmp_path.iterdir() if p.name.endswith(".gp-partial")] == []

    def test_an_edit_keeps_the_files_mode(self, tmp_path: Path) -> None:
        target = tmp_path / "run.py"
        target.write_text("x = 1\n")
        target.chmod(0o754)
        apply_changes([PlannedChange(target, "x = 2\n", original="x = 1\n")])
        assert target.read_text() == "x = 2\n"
        assert target.stat().st_mode & 0o777 == 0o754

    def test_an_edit_through_a_symlink_writes_the_target_and_keeps_the_link(
        self, tmp_path: Path
    ) -> None:
        real = tmp_path / "shared.gitignore"
        real.write_text("dist/\n")
        link = tmp_path / ".gitignore"
        link.symlink_to(real)
        apply_changes([PlannedChange(link, "dist/\ntests/captures/\n", original="dist/\n")])
        assert link.is_symlink()
        assert real.read_text() == "dist/\ntests/captures/\n"

    def test_a_dangling_symlink_is_a_conflict_for_a_create(self, tmp_path: Path) -> None:
        (tmp_path / "a.py").symlink_to(tmp_path / "missing.py")
        with pytest.raises(ChangeConflictError) as caught:
            apply_changes([PlannedChange(tmp_path / "a.py", "a = 1\n")])
        assert caught.value.conflicts == [tmp_path / "a.py"]
        assert not (tmp_path / "missing.py").exists()

    def test_an_unreadable_parent_is_a_write_error_not_a_bare_os_error(
        self, tmp_path: Path
    ) -> None:
        """No OSError escapes apply_changes unwrapped, even from the checks that run
        before the first write."""
        if os.geteuid() == 0:
            pytest.skip("root ignores mode bits")
        locked = tmp_path / "locked"
        locked.mkdir()
        locked.chmod(0o000)
        try:
            with pytest.raises(ScaffoldWriteError) as caught:
                apply_changes([PlannedChange(locked / "sub" / "a.py", "a = 1\n")])
            assert caught.value.unrestored == ()
            assert caught.value.path == locked / "sub" / "a.py"
        finally:
            locked.chmod(0o700)
        assert list(locked.iterdir()) == []


# Every module in the package but the writer itself, found rather than listed, so
# a mutator added later is covered without editing this test.
_NOT_THE_WRITER = sorted(p.name for p in _SCAFFOLD_DIR.glob("*.py") if p.name != "write.py")
_DISK_CALLS = {"write_text", "write_bytes", "touch", "unlink", "rmdir", "mkdir"}
_WRITE_MODE = re.compile(r"[wax+]")
# Modules whose import is itself a way to write: the filesystem calls of os and
# shutil, and the writing side of the captures rule, devtools/captures.py. The
# rule's pure side, devtools/captures_rule.py, is the one a scaffold module
# imports, so no list of writer names has to be kept in step with captures.py.
_WRITING_MODULES = {"os", "shutil"}
_CAPTURES_WRITING_SIDE = "graftpunk.devtools.captures"


def _tree(module: str) -> ast.Module:
    return ast.parse((_SCAFFOLD_DIR / module).read_text(encoding="utf-8"))


def _opens_for_writing(node: ast.Call) -> bool:
    """``open(path, mode)`` or ``path.open(mode)`` with a mode that writes. A mode
    that is not a string literal counts as writing."""
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        mode_index = 1
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        mode_index = 0
    else:
        return False
    modes = [k.value for k in node.keywords if k.arg == "mode"]
    if len(node.args) > mode_index:
        modes.append(node.args[mode_index])
    return any(
        not (isinstance(m, ast.Constant) and isinstance(m.value, str))
        or bool(_WRITE_MODE.search(m.value))
        for m in modes
    )


@pytest.mark.parametrize("module", _NOT_THE_WRITER)
def test_no_module_but_write_py_touches_the_disk(module: str) -> None:
    """No scaffold module but write.py calls a Path writer, opens a file to write,
    or imports os, shutil, or the writing side of the captures rule.

    What these checks cannot see: a writer reached through getattr or a string
    name, a Path method passed as a value and called elsewhere, ``open`` bound to
    another name, a subprocess that writes, and a call into any other package's
    function that writes. Review covers those."""
    writes_directly = [
        node.func.attr
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _DISK_CALLS
    ]
    assert writes_directly == []
    opened = [
        node.lineno
        for node in ast.walk(_tree(module))
        if isinstance(node, ast.Call) and _opens_for_writing(node)
    ]
    assert opened == []


def _writes(module: str) -> bool:
    return module.split(".")[0] in _WRITING_MODULES or module == _CAPTURES_WRITING_SIDE


@pytest.mark.parametrize("module", _NOT_THE_WRITER)
def test_no_module_but_write_py_imports_a_way_to_write(module: str) -> None:
    """A scaffold module takes the captures rule from captures_rule.py, whose names
    are pure, and nothing at all from captures.py."""
    found: list[str] = []
    for node in ast.walk(_tree(module)):
        if isinstance(node, ast.Import):
            found += [a.name for a in node.names if _writes(a.name)]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if _writes(node.module):
                found.append(node.module)
            elif node.module == "graftpunk.devtools":
                found += [
                    f"graftpunk.devtools.{a.name}" for a in node.names if a.name == "captures"
                ]
    assert found == []


def test_every_module_that_applies_changes_takes_them_from_write_py() -> None:
    """The routing, on the import graph: a module that applies changes imports
    apply_changes from write.py, and write_scaffold's module is one of them."""
    appliers = []
    for module in _NOT_THE_WRITER:
        tree = _tree(module)
        calls = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "apply_changes"
            for node in ast.walk(tree)
        )
        if calls:
            appliers.append(module)
            assert any(
                isinstance(node, ast.ImportFrom)
                and node.module == "graftpunk.devtools.scaffold.write"
                and any(alias.name == "apply_changes" for alias in node.names)
                for node in ast.walk(tree)
            ), module
    assert "project.py" in appliers
