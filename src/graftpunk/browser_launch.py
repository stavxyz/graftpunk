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
"""

from __future__ import annotations

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
