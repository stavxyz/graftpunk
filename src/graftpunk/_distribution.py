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
from importlib.util import find_spec

FULL = "graftpunk"
LITE = "graftpunk-lite"

# The import name of each package in pyproject.toml's [tool.graftpunk.lite]
# browser-only list, which graftpunk installs and graftpunk-lite moves into its
# [browser] extra. scripts/smoke_install.sh checks the built wheels against it.
BROWSER_MODULES = (
    "requestium",
    "selenium",
    "webdriver_manager",
    "undetected_chromedriver",
    "selenium_stealth",
    "nodriver",
    "httpie",
)


def all_installed() -> list[tuple[str, str]]:
    """The (name, version) of every graftpunk distribution installed, graftpunk
    first. More than one is unsupported but possible: both write the same files,
    and metadata cannot say whose files are on disk."""
    found: list[tuple[str, str]] = []
    for name in (FULL, LITE):
        try:
            found.append((name, _pkg_version(name)))
        except PackageNotFoundError:
            continue
    return found


def installed() -> tuple[str, str] | None:
    """The (name, version) of the installed distribution, or ``None`` when
    neither is installed (running from a source tree). graftpunk wins when
    both are installed; ``gp version`` warns about that state."""
    found = all_installed()
    return found[0] if found else None


def name() -> str:
    """The installed distribution's name, ``graftpunk`` when neither is installed."""
    found = installed()
    return found[0] if found else FULL


def has_browser_stack() -> bool:
    """Whether every browser package is importable here, found without importing
    any. A property of the environment, not of the distribution's name:
    ``graftpunk-lite[browser]`` has the stack, as ``graftpunk`` does."""
    return all(find_spec(module) is not None for module in BROWSER_MODULES)


def install_command(extra: str) -> str:
    """The pip command that adds *extra* to whichever distribution is installed."""
    return f"pip install '{name()}[{extra}]'"
