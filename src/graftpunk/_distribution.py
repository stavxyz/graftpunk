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
    return absent_browser_module() is None


def absent_browser_module(preferred: str = "") -> str | None:
    """A browser module that is not importable here, or ``None`` when the stack
    is complete. *preferred* is named when it is one of the absent ones."""
    absent = [module for module in BROWSER_MODULES if not _importable(module)]
    if preferred in absent:
        return preferred
    return absent[0] if absent else None


def missing_browser_module(exc: BaseException) -> str | None:
    """The browser module whose absence caused *exc*, or ``None`` when no
    browser module is missing.

    Follows the cause and context chain, since the library re-raises the
    original ``ModuleNotFoundError`` under its own ``ImportError`` (as
    ``load_session`` does). A missing module outside ``BROWSER_MODULES`` is not
    a browser-stack failure and gives ``None``, so its caller can re-raise it.
    Neither is a missing submodule of a browser package that is installed
    (``selenium.webdriver.x`` with selenium present): reinstalling the extra
    would not fix that version skew, and its traceback is the useful report.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ModuleNotFoundError) and current.name:
            top = current.name.partition(".")[0]
            if top in BROWSER_MODULES and not _importable(top):
                return top
        current = current.__cause__ or current.__context__
    return None


def _importable(module: str) -> bool:
    """Whether top-level *module* can be found. A module whose spec lookup
    itself fails (a ``sys.modules`` entry without ``__spec__``) is loaded, so
    it counts as importable."""
    try:
        return find_spec(module) is not None
    except (ImportError, ValueError):
        return True


def install_command(extra: str) -> str:
    """The pip command that adds *extra* to whichever distribution is installed."""
    return f"pip install '{name()}[{extra}]'"


def install_commands(extra: str) -> str:
    """The uv tool command, then the pip one, that add *extra* to whichever
    distribution is installed. A gp from ``uv tool install`` runs in its own
    environment, which the ``pip`` on PATH does not reach."""
    return f"uv tool install --force '{name()}[{extra}]' (or: {install_command(extra)})"
