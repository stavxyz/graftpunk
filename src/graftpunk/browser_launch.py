"""What every graftpunk browser launch does before it starts a browser (#96).

The composition root for the three launch sites, and the only module that knows
about settings, signal handlers and orphan cleanup at once. Keeping it separate
is what lets :mod:`graftpunk.chrome_orphans` depend on nothing but
``graftpunk.logging`` and :mod:`graftpunk.signals` know nothing about browsers.

Two entry points, because they have to run on different threads.
:func:`arm_termination_handlers` sets signal dispositions, which CPython only
allows from the main thread, so a launch site calls it directly.
:func:`prepare_browser_launch` shells out to ``ps`` and sleeps through a grace
period, so an async launch site hands it to a worker thread.

:class:`NodriverBrowserHandle` is here for the same reason: the two launch
sites that drive ``nodriver`` directly need the signal adapter
``NoDriverBackend`` gets by implementing the protocol itself, and one adapter
in the composition root is what stops them writing two.

:func:`chrome_in_own_session` is for the one launch site whose user ends the
run with Ctrl+C, ``gp observe``: it starts Chrome outside the terminal's
foreground process group (#333).
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from graftpunk import chrome_orphans, signals
from graftpunk.chrome_orphans import ChromeProcess
from graftpunk.config import get_settings
from graftpunk.logging import get_logger

LOG = get_logger(__name__)


@dataclass(frozen=True)
class CleanupReport:
    """What one pre-launch cleanup pass actually did.

    Attributes:
        reaped: The orphaned processes this pass ended.
        profiles_removed: The stale temp profile directories it deleted.
    """

    reaped: list[ChromeProcess] = field(default_factory=list)
    profiles_removed: list[Path] = field(default_factory=list)

    def __bool__(self) -> bool:
        """True when the pass changed something, so a caller can report it once."""
        return bool(self.reaped or self.profiles_removed)


class NodriverBrowserHandle:
    """A signal-handler grip on a ``nodriver`` browser started outside the backend.

    ``gp observe`` and browser token extraction call ``nodriver.start``
    themselves rather than going through ``NoDriverBackend``, so they register
    this adapter to get the same SIGTERM and SIGHUP cleanup the backend gets
    (#96). It implements :class:`graftpunk.signals.TerminatableBrowser`, and
    the sequence itself lives in
    :func:`graftpunk.chrome_orphans.terminate_nodriver_browser`, shared with
    the backend.
    """

    def __init__(self, browser: Any) -> None:
        self._browser = browser

    def _terminate_for_signal(self) -> None:
        """Send the browser process one SIGTERM, and nothing else.

        No temp profile removal: a handler runs while the process is on its way
        out, and the directory is left for a later launch's stale sweep.
        """
        chrome_orphans.terminate_nodriver_browser(self._browser)


def arm_termination_handlers() -> None:
    """Install the SIGTERM and SIGHUP handlers, if this process may have them.

    Call it from the thread that owns the process's signal dispositions:
    ``signal.signal`` raises ``ValueError`` anywhere but the main thread, and
    :func:`graftpunk.signals.install_termination_cleanup` logs that at debug
    and returns, so arming from a worker thread would silently do nothing.
    That is why this is separate from :func:`prepare_browser_launch`, which a
    launch site runs off the main thread.

    Arming happens at the first launch rather than in the CLI's root callback,
    so a command that opens no browser claims no signal slot. It is skipped
    when the orphan module is disarmed: that one flag means "this process may
    not touch signals, processes, or directories", which is what makes the
    unit suite safe without a list of things to patch.

    It never raises. A launch must not fail because a signal slot could not be
    taken.
    """
    if not (chrome_orphans.is_armed() and signals.auto_install):
        return
    try:
        signals.install_termination_cleanup()
    except Exception as exc:  # noqa: BLE001 - a launch must not fail on this
        LOG.warning("termination_handler_install_failed", error=str(exc), exc_info=True)


def prepare_browser_launch() -> CleanupReport:
    """End the orphaned Chromes and sweep the stale temp profiles.

    The one cleanup entry point all three launch sites call, so the backend,
    ``gp observe`` and browser token extraction cannot drift. It applies the
    opt-out, runs the two sweeps under separate guards so a failure in one does
    not cost the other, and never raises: a browser start must not fail because
    a cleanup pass did.

    Arming the termination handlers is :func:`arm_termination_handlers`, not
    this function: this one is written to run on a worker thread, where
    setting a signal disposition is not allowed.

    It prints nothing. The caller decides whether its user wants a line about
    it: ``gp observe`` prints one, a plugin login only logs, and neither
    decision belongs in here.

    Returns:
        What was cleaned up. Falsey when nothing was.
    """
    try:
        if get_settings().keep_orphaned_chrome:
            LOG.debug("chrome_orphan_cleanup_skipped", reason="opt_out")
            return CleanupReport()
    except Exception as exc:  # noqa: BLE001 - unreadable settings are not a launch failure
        LOG.warning("chrome_orphan_cleanup_failed", stage="settings", error=str(exc), exc_info=True)
        return CleanupReport()

    try:
        reaped = chrome_orphans.reap_orphans()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="reap", error=str(exc), exc_info=True)
        reaped = []

    try:
        profiles_removed = chrome_orphans.remove_stale_temp_profiles()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="sweep", error=str(exc), exc_info=True)
        profiles_removed = []

    return CleanupReport(reaped=reaped, profiles_removed=profiles_removed)


def browser_connect_failure_message(attempts: int) -> str:
    """The message all three launch sites raise after exhausting connect retries.

    One wording, hoisted here so the backend, ``gp observe`` and browser token
    extraction cannot drift from each other.
    """
    return (
        f"Failed to connect to browser after {attempts} attempts. "
        "Chrome may be slow to start; try again, or check for stale "
        "Chrome processes."
    )


_NODRIVER_LAUNCHER = "nodriver.core.browser"


class _AsyncioStartingNewSessions:
    """``asyncio`` as nodriver's launcher module sees it inside :func:`chrome_in_own_session`.

    Every attribute is the real module's, except that a subprocess it creates
    starts in a new session.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    async def create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*args, **kwargs)


@contextlib.contextmanager
def chrome_in_own_session() -> Iterator[bool]:
    """While open, a ``nodriver.start`` launches Chrome in a session of its own (#333).

    A terminal delivers Ctrl+C to its whole foreground process group, and
    nodriver starts Chrome in Python's group. Ignoring SIGINT around the start
    does not protect it (Chrome does not keep the ignored disposition it
    inherits), so a Ctrl+C meant to stop ``gp observe interactive`` and save the
    run also killed the browser the save reads its screenshot and page source
    from.

    nodriver has no option for this. ``Browser.start`` calls
    ``asyncio.create_subprocess_exec`` through the ``asyncio`` name bound in
    ``nodriver.core.browser``, so this rebinds that one name, for the length of
    the block, to an object that adds ``start_new_session=True``. Nothing
    outside nodriver's launcher module sees a change. A launcher whose
    ``asyncio`` is not the real module (nodriver changed, or something else
    already rebound it) is left alone with a warning, and Chrome then starts
    where it always did. ``tests/unit/test_browser_launch.py`` pins the call
    this relies on, so a nodriver release that moves it fails the suite.

    A Chrome in its own session no longer receives the terminal's SIGINT or
    SIGHUP, or a ``kill`` aimed at Python's process group. The stop path, the
    SIGTERM and SIGHUP handlers, and the orphan sweep all find it by pid and
    marker, which a session does not change.

    Yields:
        Whether the launcher was rebound.
    """
    if os.name != "posix":
        yield False
        return
    try:
        launcher = importlib.import_module(_NODRIVER_LAUNCHER)
    except ImportError as exc:
        LOG.warning("chrome_own_session_unavailable", reason="import", error=str(exc))
        yield False
        return
    if getattr(launcher, "asyncio", None) is not asyncio:
        LOG.warning("chrome_own_session_unavailable", reason="launcher_changed")
        yield False
        return
    setattr(launcher, "asyncio", _AsyncioStartingNewSessions())  # noqa: B010
    try:
        yield True
    finally:
        setattr(launcher, "asyncio", asyncio)  # noqa: B010
