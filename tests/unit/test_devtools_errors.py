"""One base class for every devtools refusal a CLI entry point reports."""

from __future__ import annotations

from pathlib import Path

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
