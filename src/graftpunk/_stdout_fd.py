"""Give ``sys.stdout`` a file descriptor while httpie is first imported.

``httpie.context`` calls ``curses.setupterm()`` in the ``Environment`` class
body, at import time. ``setupterm`` asks ``sys.stdout`` for its file
descriptor, and httpie catches only ``curses.error``, so importing it while
stdout is an ``io.StringIO`` (or anything else without ``fileno()``) raises
``io.UnsupportedOperation`` (#250). ``graftpunk.session`` is the one graftpunk
module that imports ``httpie.context``, and it does so under
:func:`stdout_with_a_file_descriptor`.
"""

from __future__ import annotations

import atexit
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import IO, Any

# The devnull stand-in, opened at most once and closed only at exit: httpie's
# Environment keeps the stdout it saw at import as a class attribute.
_devnull: IO[str] | None = None


def _has_file_descriptor(stream: Any) -> bool:
    try:
        stream.fileno()
    except (AttributeError, OSError, ValueError):
        # io.UnsupportedOperation is both an OSError and a ValueError; a
        # closed file raises ValueError; None has no fileno at all.
        return False
    return True


def _stand_in() -> IO[str]:
    global _devnull
    process_stdout = sys.__stdout__
    if process_stdout is not None and _has_file_descriptor(process_stdout):
        return process_stdout
    if _devnull is None:
        _devnull = open(os.devnull, "w")  # noqa: SIM115
        atexit.register(_devnull.close)
    return _devnull


@contextmanager
def stdout_with_a_file_descriptor() -> Iterator[None]:
    """Run the block with a ``sys.stdout`` that has a file descriptor.

    When ``sys.stdout`` already has one, nothing changes. Otherwise it points
    at the process's original stdout (``sys.__stdout__``), or at devnull when
    that has no descriptor either, and is restored when the block exits,
    whether or not it raised.
    """
    if _has_file_descriptor(sys.stdout):
        yield
        return
    original = sys.stdout
    sys.stdout = _stand_in()
    try:
        yield
    finally:
        sys.stdout = original
