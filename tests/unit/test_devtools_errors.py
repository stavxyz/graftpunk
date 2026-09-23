"""One base class for every devtools refusal a CLI entry point reports."""

from __future__ import annotations

import pickle
from pathlib import Path
from typing import TypeVar

from graftpunk.devtools.errors import DevtoolsRefusal, ScaffoldWriteError
from graftpunk.devtools.scaffold.write import ChangeConflictError, InvalidChangeError


def test_the_refusals_share_one_base_and_keep_their_own() -> None:
    for error in (ChangeConflictError, InvalidChangeError, ScaffoldWriteError):
        assert issubclass(error, DevtoolsRefusal), error
    assert issubclass(InvalidChangeError, ValueError)
    assert issubclass(ScaffoldWriteError, OSError)


def test_a_write_error_keeps_the_os_errors_fields_and_its_own_message() -> None:
    """A caller that caught the writer's OSError before this class existed reads the
    same errno, strerror, and filename it did then."""
    error = OSError(28, "No space left on device", "src/plugin.py")
    raised = ScaffoldWriteError(Path("src/plugin.py"), error)
    assert (raised.errno, raised.strerror, raised.filename) == (
        28,
        "No space left on device",
        "src/plugin.py",
    )
    assert str(raised) == (
        "Could not write src/plugin.py: No space left on device. "
        "Every file this operation had changed was restored."
    )


def test_a_refusal_before_the_first_write_says_nothing_was_written() -> None:
    denied = OSError(13, "Permission denied", "pyproject.toml")
    raised = ScaffoldWriteError(Path("pyproject.toml"), denied, before_first_write=True)
    assert str(raised) == "Could not write pyproject.toml: Permission denied. Nothing was written."


_E = TypeVar("_E", bound=BaseException)


def _round_trip(error: _E) -> _E:
    return pickle.loads(pickle.dumps(error))  # noqa: S301 - bytes this test just produced


def test_each_refusal_survives_pickling() -> None:
    """A refusal raised in a worker process reaches its parent whole."""
    write_error = ScaffoldWriteError(
        Path("src/plugin.py"),
        OSError(28, "No space left on device", "src/plugin.py"),
        (Path("src/a.py"),),
    )
    conflict = ChangeConflictError(
        [Path("a.py"), Path("b.py")], changed=(Path("b.py"),), duplicates=()
    )
    invalid = InvalidChangeError(Path("bad.py"), "the result does not parse as Python")
    refused_early = ScaffoldWriteError(
        Path("pyproject.toml"),
        OSError(13, "Permission denied", "pyproject.toml"),
        before_first_write=True,
    )
    for error in (write_error, refused_early, conflict, invalid):
        copy = _round_trip(error)
        assert type(copy) is type(error)
        assert str(copy) == str(error)
    copied_write = _round_trip(write_error)
    assert (copied_write.path, copied_write.unrestored) == (
        write_error.path,
        write_error.unrestored,
    )
    assert (copied_write.errno, copied_write.filename) == (28, "src/plugin.py")
    copied_conflict = _round_trip(conflict)
    assert (copied_conflict.conflicts, copied_conflict.changed) == (
        conflict.conflicts,
        conflict.changed,
    )
    copied_invalid = _round_trip(invalid)
    assert (copied_invalid.path, copied_invalid.reason) == (invalid.path, invalid.reason)
