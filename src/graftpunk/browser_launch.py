"""What every graftpunk browser launch does before it starts a browser (#96).

The composition root for the two launch sites, and the only module that knows
about settings, signal handlers and orphan cleanup at once. Keeping it separate
is what lets :mod:`graftpunk.chrome_orphans` depend on nothing but
``graftpunk.logging`` and :mod:`graftpunk.signals` know nothing about browsers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

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


def prepare_browser_launch() -> CleanupReport:
    """Arm the termination handlers, end orphaned Chromes, sweep stale profiles.

    The one entry point both launch sites call, so the backend and
    ``gp observe`` cannot drift. It applies the opt-out, runs the two sweeps
    under separate guards so a failure in one does not cost the other, and
    never raises: a browser start must not fail because a cleanup pass did.

    Arming happens here rather than in the CLI's root callback, so a command
    that opens no browser claims no signal slot, and it is skipped when the
    orphan module is disarmed: that one flag means "this process may not touch
    signals, processes, or directories", which is what makes the unit suite
    safe without a list of things to patch.

    It prints nothing. The caller decides whether its user wants a line about
    it: ``gp observe`` prints one, a plugin login only logs, and neither
    decision belongs in here.

    Returns:
        What was cleaned up. Falsey when nothing was.
    """
    if chrome_orphans._ARMED and signals.auto_install:
        try:
            signals.install_termination_cleanup()
        except Exception as exc:  # noqa: BLE001 - a launch must not fail on this
            LOG.warning("termination_handler_install_failed", error=str(exc))

    try:
        if get_settings().keep_orphaned_chrome:
            LOG.debug("chrome_orphan_cleanup_skipped", reason="opt_out")
            return CleanupReport()
    except Exception as exc:  # noqa: BLE001 - unreadable settings are not a launch failure
        LOG.warning("chrome_orphan_cleanup_failed", stage="settings", error=str(exc))
        return CleanupReport()

    try:
        reaped = chrome_orphans.reap_orphans()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="reap", error=str(exc))
        reaped = []

    try:
        profiles_removed = chrome_orphans.remove_stale_temp_profiles()
    except Exception as exc:  # noqa: BLE001 - cleanup must never fail a browser start
        LOG.warning("chrome_orphan_cleanup_failed", stage="sweep", error=str(exc))
        profiles_removed = []

    return CleanupReport(reaped=reaped, profiles_removed=profiles_removed)
