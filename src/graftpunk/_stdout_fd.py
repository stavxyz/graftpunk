"""Give ``sys.stdout`` a file descriptor while httpie is first imported.

``httpie.context`` calls ``curses.setupterm()`` in the ``Environment`` class
body, at import time. ``setupterm`` asks ``sys.stdout`` for its file
descriptor, and httpie catches only ``curses.error``, so importing it while
stdout is an ``io.StringIO`` (or anything else without ``fileno()``) raises
``io.UnsupportedOperation`` (#250). The same class body calls
``sys.stderr.isatty()``, which fails when ``sys.stderr`` is ``None`` (as under
``pythonw``). graftpunk imports ``httpie.context`` only inside
:func:`graftpunk.cookies.importing_httpie`, which runs the import under
:func:`stdout_with_a_file_descriptor`. (Loading a session cached by 1.21.0 or
earlier imports ``httpie.cookies`` outside it; that module does no terminal
setup.)

The swap is process-wide for the duration of the block, as with
``contextlib.redirect_stdout``: output another thread writes to ``sys.stdout``
in that window goes to the stand-in, not to the caller's stream. It happens
only when stdout has no descriptor (or stderr is ``None``), and the block is
httpie's first import, so at most once per process in practice.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, TextIO

# The devnull stand-in, opened at most once and closed only at exit: httpie's
# Environment keeps the stdout and stderr it saw at import as class attributes.
_devnull: TextIO | None = None


def _has_file_descriptor(stream: Any) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        # io.UnsupportedOperation is both an OSError and a ValueError; a
        # closed file raises ValueError; None has no fileno at all.
        return False
    return True


def open_devnull() -> TextIO:
    """One null-device file for the process. It is never closed: graftpunk's
    logging may write to it from an exit handler that runs after any close
    graftpunk could register, and the process releases it on exit."""
    global _devnull
    if _devnull is None:
        _devnull = open(os.devnull, "w")  # noqa: SIM115
    return _devnull


def _stand_in() -> TextIO:
    process_stdout = sys.__stdout__
    if process_stdout is not None and _has_file_descriptor(process_stdout):
        return process_stdout
    return open_devnull()


@contextmanager
def stdout_with_a_file_descriptor() -> Iterator[None]:
    """Run the block with a ``sys.stdout`` that has a file descriptor.

    When ``sys.stdout`` already has one, it is left alone. Otherwise it points
    at the process's original stdout (``sys.__stdout__``), or at devnull when
    that has no descriptor either. A ``None`` ``sys.stderr`` points at devnull.
    Whatever was replaced is restored when the block exits, whether or not it
    raised.
    """
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    swap_stdout = not _has_file_descriptor(original_stdout)
    swap_stderr = original_stderr is None
    # Both stand-ins exist before either stream is replaced, so a failure to
    # open devnull leaves the caller's streams as they were.
    stdout_stand_in = _stand_in() if swap_stdout else None
    stderr_stand_in = open_devnull() if swap_stderr else None
    if stdout_stand_in is not None:
        sys.stdout = stdout_stand_in
    if stderr_stand_in is not None:
        sys.stderr = stderr_stand_in
    try:
        yield
    finally:
        if swap_stdout:
            sys.stdout = original_stdout
        if swap_stderr:
            sys.stderr = original_stderr
