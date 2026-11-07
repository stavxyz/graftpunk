"""The one rule for what a fixtures check's ``skip`` accepts (#370).

``graftpunk.testing.plugin``'s ``fixtures_are_sanitised`` and ``check_fixtures_tree``
refuse a bad ``skip`` with it, and the plugin devtools report a suite conftest's
``SUITE_MEMBER_FIXTURES`` entry it refuses. Its own module, importing nothing but
the standard library, so the devtools read it without importing the pytest plugin.
"""

from __future__ import annotations

import os
from collections.abc import Iterable

__all__ = ["refuses", "skip_names"]

_SEPARATORS = frozenset({"/", "\\", os.sep, os.altsep or "/"})


def refuses(name: object) -> bool:
    """Whether *name* cannot be a ``skip`` entry: anything but a string, an empty
    name, ``.``, ``..``, or a name with a path separator. A name with no matching
    directory is not refused."""
    if not isinstance(name, str):
        return True
    return name in ("", ".", "..") or any(sep in name for sep in _SEPARATORS)


def skip_names(skip: Iterable[str]) -> frozenset[str]:
    """*skip* as the set of directory names it holds, refusing what can never name a
    directory directly under a tree, so a mistake is an error rather than a check
    that quietly covers more than intended.

    Raises:
        TypeError: *skip* is a ``str`` or ``bytes`` (which would iterate as
            characters), or holds something other than a ``str``.
        ValueError: An entry is empty, ``.`` or ``..``, or holds a path separator.
    """
    if isinstance(skip, (str, bytes)):
        raise TypeError(
            f"skip must be a collection of directory names, not {type(skip).__name__} "
            f"{skip!r}; write ({skip!r},) for one name."
        )
    names = tuple(skip)
    for name in names:
        if not isinstance(name, str):
            raise TypeError(f"skip holds {name!r}, not a directory name (str).")
        if refuses(name):
            raise ValueError(
                f"skip holds {name!r}; each entry names one directory directly under the "
                "tree, with no path separator."
            )
    return frozenset(names)
