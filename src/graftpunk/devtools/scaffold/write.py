"""The one way ``graftpunk.devtools.scaffold`` writes to a developer's project.

A change is planned against the text on disk, refused with a reason if it
conflicts, rendered in full, validated before anything is written by the
validator the change carries, written atomically, and restored from the
original text if any later write of the same operation fails. The writer never
switches on file type: a Python module's change carries :func:`validate_python`,
a ``pyproject.toml``'s carries :func:`validate_toml`, and a file with no grammar
(a README, a ``.gitkeep``) carries none (graft skill spec, 2026-09-21).

Imported by the writers only (the scaffold, the stub inserter, the migrator);
the project reader and the lint never import it.
"""

from __future__ import annotations

import ast
import contextlib
import os
import stat
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from graftpunk.devtools.errors import DevtoolsRefusal, ScaffoldWriteError

__all__ = [
    "ChangeConflictError",
    "InvalidChangeError",
    "PlannedChange",
    "Validator",
    "apply_changes",
    "find_conflicts",
    "read_original",
    "validate_python",
    "validate_toml",
]

Validator = Callable[[str], None]

_PARTIAL_SUFFIX = ".gp-partial"


def validate_python(text: str) -> None:
    """Raise ``ValueError`` unless *text* parses as a Python module."""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        raise ValueError(f"does not parse as Python: {exc.msg} (line {exc.lineno})") from exc


def validate_toml(text: str) -> None:
    """Raise ``ValueError`` unless *text* loads as TOML."""
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"does not load as TOML: {exc}") from exc


@dataclass(frozen=True)
class PlannedChange:
    """One file's full new content, planned against what the file held.

    ``original`` is ``None`` for a create, which conflicts with any file already
    at ``path``; for an edit it is the text the plan was made from, read with
    :func:`read_original`, and the change conflicts if the file no longer holds
    exactly that.
    """

    path: Path
    content: str
    original: str | None = None
    validate: Validator | None = None


class ChangeConflictError(DevtoolsRefusal):
    """One or more planned changes conflict with the disk; nothing was written."""

    def __init__(self, conflicts: list[Path]) -> None:
        self.conflicts = conflicts
        listing = ", ".join(str(p) for p in conflicts)
        super().__init__(f"Refusing to overwrite existing file(s): {listing}")


class InvalidChangeError(DevtoolsRefusal, ValueError):
    """A planned change's content, or the file it was planned from, is not text the
    writer can use; nothing was written. *reason* is a clause with its own subject
    ("the result does not parse ...", "it is not UTF-8 text")."""

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.reason = reason
        super().__init__(f"Refusing to write {path}: {reason}")


def read_original(path: Path) -> str:
    """*path*'s text exactly as it is on disk, for a :class:`PlannedChange`'s
    ``original``. Decoded from bytes, so no line ending is translated: a CRLF file
    is compared and restored as CRLF.

    Raises:
        InvalidChangeError: The file is not UTF-8 text.
        OSError: The file cannot be read.
    """
    try:
        return path.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidChangeError(path, "it is not UTF-8 text") from exc


def _holds(path: Path, text: str) -> bool:
    """True when *path* is a file whose text is exactly *text*; False when it is
    not, or cannot be read."""
    try:
        return path.read_bytes().decode("utf-8") == text
    except (OSError, UnicodeDecodeError):
        return False


def _conflicts(change: PlannedChange) -> bool:
    if change.original is None:
        # lexists, not Path.exists: it never raises (an unreadable parent is the
        # write's failure to report, with its own OS error), and a dangling
        # symlink at the path is a conflict, not a free slot.
        return os.path.lexists(change.path)
    return not _holds(change.path, change.original)


def find_conflicts(changes: Sequence[PlannedChange]) -> list[Path]:
    """The paths among *changes* that conflict with the disk, sorted."""
    return sorted(change.path for change in changes if _conflicts(change))


def _missing_parents(directory: Path) -> list[Path]:
    """The ancestors of *directory*, *directory* included, that do not exist yet,
    deepest last: the order ``mkdir(parents=True)`` creates them. A dangling
    symlink counts as existing: ``mkdir`` then fails on it, and the restore must
    not list a path this operation never created."""
    missing: list[Path] = []
    current = directory
    while not os.path.lexists(current) and current != current.parent:
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def _write_atomically(path: Path, text: str) -> None:
    """Write *text* to a sibling temp file and move it over *path*, so a reader never
    sees a half-written module. The temp file is removed if either step fails.

    A symlinked *path* is written through, so the link survives, and an existing
    file keeps its permission bits; ``os.replace`` alone would swap in a new
    regular file with the umask's mode.
    """
    target = Path(os.path.realpath(path))
    partial = target.with_name(f".{target.name}{_PARTIAL_SUFFIX}")
    try:
        try:
            mode: int | None = stat.S_IMODE(target.stat().st_mode)
        except FileNotFoundError:
            mode = None
        # newline="": the text is written as given, so an original read by
        # read_original comes back byte for byte.
        partial.write_text(text, encoding="utf-8", newline="")
        if mode is not None:
            partial.chmod(mode)
        os.replace(partial, target)
    except BaseException:
        # Any exception, KeyboardInterrupt included: the partial is this
        # function's own, and nothing else knows to remove it.
        with contextlib.suppress(OSError):
            partial.unlink()
        raise


def _restore(started: list[PlannedChange], created_dirs: list[Path]) -> list[Path]:
    """Put back what this operation changed, best effort, and return every path it
    could not put back: each edited file's original text, each created file
    removed, then each directory this operation created, deepest first and only
    while empty. *started* ends with the change whose write failed, which may or
    may not have reached the disk.

    An edit is put back atomically, and only when the file no longer holds its
    original text: rewriting it in place would truncate a file the failed write
    left intact, on the same full disk that made it fail. Every step is guarded,
    because unwinding runs in the conditions that caused the failure and the
    original error is the one the caller must see; a step that fails is reported
    in the return value instead.
    """
    unrestored: list[Path] = []
    for change in reversed(started):
        try:
            if change.original is None:
                change.path.unlink(missing_ok=True)
            elif not _holds(change.path, change.original):
                _write_atomically(change.path, change.original)
        except OSError:
            if change.original is not None or os.path.lexists(change.path):
                unrestored.append(change.path)
    for directory in reversed(created_dirs):
        try:
            directory.rmdir()
        except OSError:
            if os.path.lexists(directory):
                unrestored.append(directory)
    return unrestored


def apply_changes(changes: Sequence[PlannedChange]) -> tuple[Path, ...]:
    """Apply *changes* in order, all or nothing.

    Raises:
        ChangeConflictError: A create's path exists or an edit's file changed
            since it was planned. Raised before anything is touched.
        InvalidChangeError: A change's content cannot be encoded as UTF-8 or
            fails its validator. Raised before anything is touched.
        ScaffoldWriteError: A write failed. Every change already applied is
            undone first where it can be; the error names the path, carries the
            ``OSError``, and lists in ``unrestored`` anything left changed.

    Any other exception raised mid-write (``KeyboardInterrupt`` included) is
    re-raised as itself after the same restore, with a note naming any path
    left changed.
    """
    conflicts = find_conflicts(changes)
    if conflicts:
        raise ChangeConflictError(conflicts)
    for change in changes:
        try:
            change.content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise InvalidChangeError(
                change.path,
                f"the result cannot be encoded as UTF-8: {exc.reason} at position {exc.start}",
            ) from exc
        if change.validate is not None:
            try:
                change.validate(change.content)
            except ValueError as exc:
                raise InvalidChangeError(change.path, f"the result {exc}") from exc
    started: list[PlannedChange] = []
    created_dirs: list[Path] = []
    for change in changes:
        try:
            created_dirs.extend(_missing_parents(change.path.parent))
            change.path.parent.mkdir(parents=True, exist_ok=True)
            started.append(change)
            _write_atomically(change.path, change.content)
        except OSError as exc:
            unrestored = _restore(started, created_dirs)
            raise ScaffoldWriteError(change.path, exc, unrestored) from exc
        except BaseException as exc:
            # Not a write failure (a bug, or KeyboardInterrupt): restore all the
            # same, then let the exception itself reach the caller.
            unrestored = _restore(started, created_dirs)
            if unrestored:
                listing = ", ".join(str(p) for p in unrestored)
                exc.add_note(f"These paths could not be restored and are left changed: {listing}.")
            raise
    return tuple(change.path for change in changes)
