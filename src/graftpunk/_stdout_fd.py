"""Give ``sys.stdout`` a file descriptor while httpie is first imported.

``httpie.context`` calls ``curses.setupterm()`` in the ``Environment`` class
body, at import time. ``setupterm`` asks ``sys.stdout`` for its file
descriptor, and httpie catches only ``curses.error``, so importing it while
stdout is an ``io.StringIO`` (or anything else without ``fileno()``) raises
``io.UnsupportedOperation`` (#250). The same class body calls
``sys.stderr.isatty()``, which fails when ``sys.stderr`` is ``None`` (as under
``pythonw``). ``graftpunk.session`` is the one graftpunk module that imports
``httpie.context``, and it does so under :func:`stdout_with_a_file_descriptor`.

The swap is process-wide for the duration of the block, as with
``contextlib.redirect_stdout``: output another thread writes to ``sys.stdout``
in that window goes to the stand-in, not to the caller's stream. It happens
only when stdout has no descriptor (or stderr is ``None``), and the block is
httpie's first import, so at most once per process in practice.
"""

from __future__ import annotations

import atexit
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any

# The devnull stand-in, opened at most once and closed only at exit: httpie's
# Environment keeps the stdout and stderr it saw at import as class attributes.
_devnull: IO[str] | None = None


def _has_file_descriptor(stream: Any) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        # io.UnsupportedOperation is both an OSError and a ValueError; a
        # closed file raises ValueError; None has no fileno at all.
        return False
    return True


def _open_devnull() -> IO[str]:
    global _devnull
    if _devnull is None:
        _devnull = open(os.devnull, "w")  # noqa: SIM115
        atexit.register(_devnull.close)
    return _devnull


def _stand_in() -> IO[str]:
    process_stdout = sys.__stdout__
    if process_stdout is not None and _has_file_descriptor(process_stdout):
        return process_stdout
    return _open_devnull()


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
    if swap_stdout:
        sys.stdout = _stand_in()
    if swap_stderr:
        sys.stderr = _open_devnull()
    try:
        yield
    finally:
        if swap_stdout:
            sys.stdout = original_stdout
        if swap_stderr:
            sys.stderr = original_stderr
