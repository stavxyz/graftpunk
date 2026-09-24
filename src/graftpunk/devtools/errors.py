"""The one base class of every refusal ``graftpunk.devtools`` reports to a person.

A refusal means the operation left the disk as it found it, except for any path
a :class:`ScaffoldWriteError` names as left changed, and the message says why.
Each refusal keeps its own class, and its own second base where it has one, for
callers that want it; a ``gp plugin`` entry point catches :class:`DevtoolsRefusal`
alone.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

__all__ = ["DevtoolsRefusal", "ScaffoldWriteError"]


class DevtoolsRefusal(Exception):  # noqa: N818 - the name a later plan consumes; a refusal, not a fault
    """A devtools operation refused, and the message says why. The disk is as it was,
    except for any path a :class:`ScaffoldWriteError` names as left changed."""


class ScaffoldWriteError(DevtoolsRefusal, OSError):
    """A write failed, or was refused, during an operation. After a failure partway
    through, the writer put back every change it had already applied that it
    could; ``unrestored`` lists the paths it could not, and the message names them.
    ``before_first_write`` marks a refusal raised before any byte was written (a
    read-only edit target), whose message says nothing was written. Also an
    ``OSError`` carrying the wrapped error's ``errno`` and ``strerror``, with
    ``filename`` the path the operation meant to write (never the writer's temp
    file), so a caller that caught the writer's ``OSError`` before this class
    existed still catches it and reads the same fields."""

    def __init__(
        self,
        path: Path,
        error: OSError,
        unrestored: Sequence[Path] = (),
        *,
        before_first_write: bool = False,
    ) -> None:
        # OSError's own constructor sets errno, strerror, and filename from these
        # three arguments; __str__ keeps the one-line refusal as the message.
        super().__init__(error.errno, error.strerror, str(path))
        self.path = path
        self.error = error
        self.unrestored = tuple(unrestored)
        self.before_first_write = before_first_write
        reason = error.strerror or str(error)
        if before_first_write:
            aftermath = "Nothing was written."
        elif self.unrestored:
            listing = ", ".join(str(p) for p in self.unrestored)
            aftermath = f"These paths could not be restored and are left changed: {listing}."
        else:
            aftermath = "Every file this operation had changed was restored."
        self._message = f"Could not write {path}: {reason}. {aftermath}"

    def __str__(self) -> str:
        return self._message

    def __reduce__(
        self,
    ) -> tuple[Callable[..., ScaffoldWriteError], tuple[Path, OSError, tuple[Path, ...], bool]]:
        # OSError's own __reduce__ would rebuild from (errno, strerror, filename),
        # which is not this constructor's signature; the keyword-only flag goes
        # through _rebuild_write_error, since pickle passes arguments positionally.
        return _rebuild_write_error, (
            self.path,
            self.error,
            self.unrestored,
            self.before_first_write,
        )


def _rebuild_write_error(
    path: Path, error: OSError, unrestored: tuple[Path, ...], before_first_write: bool
) -> ScaffoldWriteError:
    return ScaffoldWriteError(path, error, unrestored, before_first_write=before_first_write)
