"""Which distribution installed this package: graftpunk or graftpunk-lite.

Both ship the same ``graftpunk`` import package (see scripts/build_lite.py), so
code that reads package metadata or tells the user what to install asks here
instead of assuming the name ``graftpunk``. Telling a graftpunk-lite user to run
``pip install 'graftpunk[browser]'`` would install the second distribution over
the same files.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

FULL = "graftpunk"
LITE = "graftpunk-lite"


def installed() -> tuple[str, str] | None:
    """The (name, version) of the installed distribution, or ``None`` when
    neither is installed (running from a source tree). graftpunk wins when
    both are installed, which is unsupported but possible."""
    for name in (FULL, LITE):
        try:
            return name, _pkg_version(name)
        except PackageNotFoundError:
            continue
    return None


def install_command(extra: str) -> str:
    """The pip command that adds *extra* to whichever distribution is installed."""
    found = installed()
    name = found[0] if found else FULL
    return f"pip install '{name}[{extra}]'"
