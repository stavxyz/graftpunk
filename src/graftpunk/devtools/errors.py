"""The one base class of every refusal ``graftpunk.devtools`` reports to a person.

A refusal means the operation left the disk as it found it, except for any path
a :class:`ScaffoldWriteError` names as left changed, and the message says why.
Each refusal keeps its own class, and its own second base where it has one, for
callers that want it; a ``gp plugin`` entry point catches :class:`DevtoolsRefusal`
alone.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

__all__ = ["DevtoolsRefusal", "ScaffoldWriteError"]


class DevtoolsRefusal(Exception):  # noqa: N818 - the name a later plan consumes; a refusal, not a fault
    """A devtools operation refused, and the message says why. The disk is as it was,
    except for any path a :class:`ScaffoldWriteError` names as left changed."""


class ScaffoldWriteError(DevtoolsRefusal, OSError):
    """A write failed partway through an operation. The writer then put back every
    change it had already applied that it could; ``unrestored`` lists the paths it
    could not, and the message names them. Also an ``OSError`` carrying the wrapped
    error's ``errno`` and ``strerror``, with ``filename`` the path the operation
    meant to write (never the writer's temp file), so a caller that caught the
    writer's ``OSError`` before this class existed still catches it and reads the
    same fields."""

    def __init__(self, path: Path, error: OSError, unrestored: Sequence[Path] = ()) -> None:
        # OSError's own constructor sets errno, strerror, and filename from these
        # three arguments; __str__ keeps the one-line refusal as the message.
        super().__init__(error.errno, error.strerror, str(path))
        self.path = path
        self.error = error
        self.unrestored = tuple(unrestored)
        reason = error.strerror or str(error)
        if self.unrestored:
            listing = ", ".join(str(p) for p in self.unrestored)
            aftermath = f"These paths could not be restored and are left changed: {listing}."
        else:
            aftermath = "Every file this operation had changed was restored."
        self._message = f"Could not write {path}: {reason}. {aftermath}"

    def __str__(self) -> str:
        return self._message

    def __reduce__(self) -> tuple[type[ScaffoldWriteError], tuple[Path, OSError, tuple[Path, ...]]]:
        # OSError's own __reduce__ would rebuild from (errno, strerror, filename),
        # which is not this constructor's signature.
        return type(self), (self.path, self.error, self.unrestored)
