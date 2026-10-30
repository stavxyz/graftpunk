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

:func:`chrome_in_own_session` is for the launch sites whose user can end a
run with Ctrl+C and still expects it saved, ``gp observe`` and
``NoDriverBackend`` (a login recorded with ``--observe full``): it starts
Chrome outside the terminal's foreground process group (#333).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib
import os
import sys
import threading
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

# What a Chrome that shares this process's session costs a user who ends a
# recording with Ctrl+C (#333). One wording for the log and the console.
CHROME_SHARES_SESSION_CONSEQUENCE = (
    "Chrome did not start in a session of its own, so a Ctrl+C in this terminal "
    "also stops Chrome, and the run's page source and screenshot can be lost."
)

_END_TERM_TIMEOUT_S = 5.0
_END_KILL_TIMEOUT_S = 2.0


class _LaunchedChromeHandle:
    """A signal-handler grip on a Chrome process from the moment it is spawned.

    nodriver spawns Chrome and then spends up to a few seconds connecting to it
    before ``nodriver.start`` returns the browser a caller can register. A
    SIGTERM or SIGHUP in that window, or while a failed attempt's Chrome is
    being ended, must still end it: a Chrome in a session of its own no longer
    receives the terminal's signals directly.
    """

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process

    def _terminate_for_signal(self) -> None:
        chrome_orphans.terminate_nodriver_browser(self)


@dataclass
class OwnSessionLaunch:
    """The Chromes spawned inside one :func:`chrome_in_own_session` block.

    Attributes:
        applied: Whether a launch in the block starts in a new session.
        processes: Every process nodriver spawned in the block, in order.
    """

    applied: bool
    processes: list[asyncio.subprocess.Process] = field(default_factory=list)
    _handles: list[_LaunchedChromeHandle] = field(default_factory=list, repr=False)

    def _record(self, process: asyncio.subprocess.Process) -> None:
        handle = _LaunchedChromeHandle(process)
        # Held here too: the signal registry holds its handles weakly.
        self._handles.append(handle)
        self.processes.append(process)
        signals.register_live_browser(handle)

    def _release(self) -> None:
        for handle in self._handles:
            signals.unregister_live_browser(handle)
        self._handles.clear()

    async def end_launched(self) -> None:
        """End every Chrome this block spawned, for a start that failed. Never raises.

        SIGTERM, up to ``_END_TERM_TIMEOUT_S`` for it to exit, then SIGKILL.
        The browser nodriver added to its registry for each is taken out of
        it, so nodriver's ``atexit`` handler does not stop it a second time,
        and its temp profile is removed once Chrome has exited.
        """
        for process in self.processes:
            try:
                await _end_process(process)
            except Exception as exc:  # noqa: BLE001 - best-effort: this runs on a failure path
                LOG.warning("failed_launch_end_failed", pid=process.pid, error=str(exc))
        for browser in _take_from_nodriver_registry(self.processes):
            if browser._process.returncode is not None:
                chrome_orphans.remove_browser_temp_profile(browser)


async def _end_process(process: asyncio.subprocess.Process) -> None:
    """SIGTERM *process*, then SIGKILL it if it has not exited in time."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        process.terminate()
    try:
        await asyncio.wait_for(process.wait(), _END_TERM_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), _END_KILL_TIMEOUT_S)


def _take_from_nodriver_registry(processes: list[asyncio.subprocess.Process]) -> list[Any]:
    """Remove from nodriver's registry, and return, the browsers running *processes*."""
    try:
        from nodriver.core.util import get_registered_instances

        registry = get_registered_instances()
    except Exception:  # noqa: BLE001 - best-effort: a missing registry only costs the cleanup
        LOG.debug("nodriver_registry_unavailable", exc_info=True)
        return []
    taken = [
        browser
        for browser in set(registry)
        if any(getattr(browser, "_process", None) is process for process in processes)
    ]
    for browser in taken:
        registry.discard(browser)
    return taken


class _AsyncioStartingNewSessions:
    """``asyncio`` as nodriver's launcher module sees it inside :func:`chrome_in_own_session`.

    Every attribute is the real module's, except that a subprocess it creates
    starts in a new session and is recorded on the open block's
    :class:`OwnSessionLaunch`.
    """

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)

    @staticmethod
    async def create_subprocess_exec(*args: Any, **kwargs: Any) -> asyncio.subprocess.Process:
        kwargs["start_new_session"] = True
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        launch = _CURRENT_LAUNCH.get()
        if launch is not None:
            launch._record(process)
        return process


_PROXY = _AsyncioStartingNewSessions()
_CURRENT_LAUNCH: contextvars.ContextVar[OwnSessionLaunch | None] = contextvars.ContextVar(
    "graftpunk_own_session_launch", default=None
)
# How many open blocks share the proxy. The first in installs it and the last
# out puts the real module back, so overlapping blocks, in one loop or on two
# threads, all launch in a new session.
_PROXY_LOCK = threading.Lock()
_proxy_users = 0


def _install_proxy() -> bool:
    """Bind the proxy in nodriver's launcher, or join the blocks already using it."""
    global _proxy_users
    try:
        launcher = importlib.import_module(_NODRIVER_LAUNCHER)
    except ImportError as exc:
        LOG.warning(
            "chrome_own_session_unavailable",
            reason="import",
            error=str(exc),
            consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
        )
        return False
    with _PROXY_LOCK:
        current = getattr(launcher, "asyncio", None)
        if current is not _PROXY:
            if current is not asyncio:
                LOG.warning(
                    "chrome_own_session_unavailable",
                    reason="launcher_changed",
                    consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
                )
                return False
            setattr(launcher, "asyncio", _PROXY)  # noqa: B010
        _proxy_users += 1
        return True


def _remove_proxy() -> None:
    """Leave the proxy; the last block out puts the real module back."""
    global _proxy_users
    launcher = sys.modules.get(_NODRIVER_LAUNCHER)
    with _PROXY_LOCK:
        _proxy_users -= 1
        # Only the proxy is replaced: a name something else bound meanwhile is
        # not this block's to overwrite.
        if _proxy_users == 0 and getattr(launcher, "asyncio", None) is _PROXY:
            setattr(launcher, "asyncio", asyncio)  # noqa: B010


@contextlib.contextmanager
def chrome_in_own_session() -> Iterator[OwnSessionLaunch]:
    """While open, a ``nodriver.start`` launches Chrome in a session of its own (#333).

    A terminal delivers Ctrl+C to its whole foreground process group, and
    nodriver starts Chrome in Python's group. Ignoring SIGINT around the start
    did not protect it (Chrome died of the keypress with the ignored
    disposition inherited), so a Ctrl+C meant to stop ``gp observe
    interactive``, or a login recorded with ``--observe full``, and save the
    run also killed the browser the save reads its screenshot and page source
    from.

    nodriver has no option for this. ``Browser.start`` calls
    ``asyncio.create_subprocess_exec`` through the ``asyncio`` name bound in
    ``nodriver.core.browser``, so this rebinds that one name, while any block
    is open, to an object that adds ``start_new_session=True``. Nothing
    outside nodriver's launcher module sees a change, but every launch through
    it while a block is open, on any thread, starts in a new session. Blocks
    may overlap: the first in installs the object and the last out restores
    the module. A launcher whose ``asyncio`` is neither the real module nor
    that object (nodriver changed, or something else rebound it) is left
    alone with a warning, and Chrome then starts where it always did.
    ``tests/unit/test_browser_launch.py`` pins the call this relies on, and
    :func:`check_chrome_in_own_session` checks the outcome after a launch.

    Each process spawned in the block is registered with the SIGTERM and
    SIGHUP handlers at once, and unregistered when the block exits, so a
    caller registers the browser it keeps before leaving the block, and ends
    a failed attempt's Chrome inside it (:meth:`OwnSessionLaunch.end_launched`).

    A Chrome in its own session no longer receives the terminal's SIGINT,
    SIGHUP or SIGQUIT, or a ``kill`` aimed at Python's process group. The stop
    path, the SIGTERM and SIGHUP handlers, and the orphan sweep all find it by
    pid and marker, which a session does not change.

    Yields:
        The block's :class:`OwnSessionLaunch`.
    """
    applied = os.name == "posix" and _install_proxy()
    launch = OwnSessionLaunch(applied=applied)
    token = _CURRENT_LAUNCH.set(launch)
    try:
        yield launch
    finally:
        # A coroutine closed from outside its task (its loop is gone) exits
        # the block in another context, where the variable needs no reset.
        with contextlib.suppress(ValueError):
            _CURRENT_LAUNCH.reset(token)
        launch._release()
        if applied:
            _remove_proxy()


def chrome_leads_own_session(browser: Any) -> bool | None:
    """Whether *browser*'s Chrome leads a session of its own; None when that cannot be told."""
    pid = getattr(getattr(browser, "_process", None), "pid", None)
    getsid = getattr(os, "getsid", None)
    if not isinstance(pid, int) or getsid is None:
        return None
    try:
        return getsid(pid) == pid
    except OSError:
        return None


def check_chrome_in_own_session(browser: Any) -> bool:
    """Check the outcome of :func:`chrome_in_own_session` on a started *browser*.

    Logs a warning that says what is lost when Chrome shares this process's
    session.

    Returns:
        False when Chrome is known to share the session, True otherwise.
    """
    if chrome_leads_own_session(browser) is not False:
        return True
    LOG.warning(
        "chrome_not_in_own_session",
        pid=browser._process.pid,
        consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
    )
    return False
