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
Chrome outside the terminal's foreground process group (#333). The backend
uses it only when graftpunk's termination handlers are installed, since
nothing else would end that Chrome on a hangup. :func:`selenium_in_own_session`
does the same for the selenium backend's undetected-chromedriver launch, which
starts chromedriver as well as Chrome (#335), under the same rule
(:func:`prepare_selenium_launch`).
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import importlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Iterator
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
_END_POLL_S = 0.05


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
    """SIGTERM *process*, then SIGKILL it if it has not exited in time.

    Polls ``returncode``, which the loop's child watcher sets at exit, as the
    backend's reap does: ``Process.wait()`` on CPython 3.11 and later also
    waits for every pipe to close, and Chrome's helper processes can hold
    nodriver's pipes open after Chrome itself has exited. Once the status is
    collected the transport is closed, which closes those pipes.
    """
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        if not await _exited_within(process, _END_TERM_TIMEOUT_S):
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await _exited_within(process, _END_KILL_TIMEOUT_S)
    if process.returncode is not None:
        transport = getattr(process, "_transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()


async def _exited_within(process: asyncio.subprocess.Process, timeout_s: float) -> bool:
    """Whether the child watcher collects *process*'s exit status within *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while process.returncode is None:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_END_POLL_S)
    return True


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
    pid and marker, which a session does not change; the handlers exist only
    in ``gp`` and in a program that called
    :func:`graftpunk.signals.install_termination_cleanup`, so a caller without
    them should not open this block.

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


# The selenium launch (#335). undetected-chromedriver starts Chrome itself and
# then has Selenium start chromedriver, each with one subprocess.Popen call
# through the ``subprocess`` name bound in its own module. The value says
# whether a process spawned there gets the owner marker appended: Chrome gets
# it through its options, and chromedriver, whose Service undetected-chromedriver
# builds without arguments a caller could add to, gets it here.
_SELENIUM_LAUNCHERS = {
    "undetected_chromedriver": False,
    "selenium.webdriver.common.service": True,
}


class SeleniumProcesses:
    """The chromedriver and Chrome of one selenium launch in a session of its own (#335).

    A :class:`graftpunk.signals.TerminatableBrowser`: registered with the
    SIGTERM and SIGHUP handlers as soon as its first process is added, and
    released by the code that quits the driver. A process graftpunk's launch
    spawned is held as its ``Popen``, so a handler never signals one that has
    exited and been reaped. A Chrome that chromedriver spawned is known by pid
    only, so it is signaled only while the process table still shows that pid
    with this process's owner marker or as the child of a held chromedriver
    (:meth:`_still_ours`); a pid reused by another program meanwhile is left
    alone.
    """

    def __init__(self) -> None:
        self._processes: list[subprocess.Popen[Any]] = []
        self._pids: list[int] = []

    def add(self, process: subprocess.Popen[Any]) -> None:
        """Hold *process*, and make sure the handlers can end it."""
        self._processes.append(process)
        signals.register_live_browser(self)

    def add_pid(self, pid: int) -> None:
        """Hold a process this program did not spawn (a Chrome under chromedriver)."""
        self._pids.append(pid)
        signals.register_live_browser(self)

    @property
    def pids(self) -> list[int]:
        """Every process held, the spawned ones first."""
        return [process.pid for process in self._processes] + list(self._pids)

    def _still_ours(self) -> list[int]:
        """The processes known by pid that are still this launch's, from one ``ps`` listing.

        A pid held by :meth:`add_pid` counts while it is listed with this
        process's owner marker or as the child of a held chromedriver; a
        browser whose parent is a held chromedriver counts too, which is how a
        Chrome spawned by a start that then failed is found. That is the
        reaper's matching (the marker, the debugging port), so a pid that
        another program has reused since is skipped. An unreadable process
        table yields nothing: not signaling is the safe direction.
        """
        drivers = {process.pid for process in self._processes if process.poll() is None}
        held = set(self._pids)
        me = os.getpid()
        return [
            proc.pid
            for proc in chrome_orphans.list_chrome_processes()
            if proc.ppid in drivers or (proc.pid in held and proc.owner_pid == me)
        ]

    def _terminate_for_signal(self) -> None:
        """Send each process still running, and still ours, one SIGTERM, and nothing else."""
        live = [process.pid for process in self._processes if process.poll() is None]
        for pid in [*live, *self._still_ours()]:
            chrome_orphans.terminate_pid(pid)

    def end(self) -> None:
        """End every process, for a start that failed or was interrupted. Never raises.

        The processes known by pid, the Chromes a held chromedriver spawned
        among them, are looked up first (:meth:`_still_ours`): a SIGTERM to
        chromedriver leaves its Chrome running. Then SIGTERM to all, up to
        ``_END_TERM_TIMEOUT_S`` for each spawned process to exit, and SIGKILL
        for one that has not.
        """
        try:
            for pid in self._still_ours():
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
            for process in self._processes:
                if process.poll() is None:
                    with contextlib.suppress(OSError):
                        process.terminate()
            for process in self._processes:
                try:
                    process.wait(_END_TERM_TIMEOUT_S)
                except subprocess.TimeoutExpired:
                    with contextlib.suppress(OSError):
                        process.kill()
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        process.wait(_END_KILL_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - best-effort: this runs on a failure path
            LOG.warning("failed_launch_end_failed", pids=self.pids, error=str(exc))
        finally:
            self.release()

    def release(self) -> None:
        """Leave the signal registry: the driver was quit, or the start failed."""
        signals.unregister_live_browser(self)


# The attribute a selenium driver started in a session of its own carries its
# SeleniumProcesses under, so whoever quits the driver can release them, and
# the registry entry (held weakly) lives no longer than the driver object.
_DRIVER_PROCESSES_ATTR = "_graftpunk_processes"


def attach_selenium_processes(driver: Any, processes: SeleniumProcesses) -> None:
    """Keep *processes* on *driver*, for :func:`release_selenium_processes`."""
    setattr(driver, _DRIVER_PROCESSES_ATTR, processes)


def release_selenium_processes(driver: Any) -> None:
    """Take a quit *driver*'s processes out of the signal registry. Never raises."""
    processes = getattr(driver, _DRIVER_PROCESSES_ATTR, None)
    if isinstance(processes, SeleniumProcesses):
        processes.release()


def chromes_started_by(pid: int) -> list[int]:
    """The browser processes, those given a debugging port, whose parent is *pid*."""
    return [
        proc.pid
        for proc in chrome_orphans.list_chrome_processes()
        if proc.ppid == pid and "--remote-debugging-port=" in proc.args
    ]


@dataclass
class SeleniumLaunch:
    """One :func:`selenium_in_own_session` block.

    Attributes:
        applied: Whether a launch in the block starts in a new session.
        processes: What the block spawned, registered with the handlers.
    """

    applied: bool
    processes: SeleniumProcesses = field(default_factory=SeleniumProcesses)


class _SubprocessStartingNewSessions:
    """``subprocess`` as a selenium launcher module sees it while a block is open.

    Every attribute is the real module's. ``Popen`` in a context with an open
    :func:`selenium_in_own_session` block starts the process in a new session,
    appends the owner marker when *mark* is set, and records the process on
    the block; in any other context, another thread's launch included, it is
    the real ``Popen`` called unchanged.
    """

    def __init__(self, *, mark: bool) -> None:
        self._mark = mark

    def __getattr__(self, name: str) -> Any:
        return getattr(subprocess, name)

    def Popen(self, args: Any, *rest: Any, **kwargs: Any) -> subprocess.Popen[Any]:  # noqa: N802
        launch = _CURRENT_SELENIUM_LAUNCH.get()
        if launch is None:
            return subprocess.Popen(args, *rest, **kwargs)  # noqa: S603 - the library's own call
        kwargs["start_new_session"] = True
        if self._mark and isinstance(args, list | tuple):
            args = [*args, chrome_orphans.owner_switch()]
        process = subprocess.Popen(args, *rest, **kwargs)  # noqa: S603 - the library's own call
        launch.processes.add(process)
        return process


_SELENIUM_PROXIES = {
    name: _SubprocessStartingNewSessions(mark=mark) for name, mark in _SELENIUM_LAUNCHERS.items()
}
_CURRENT_SELENIUM_LAUNCH: contextvars.ContextVar[SeleniumLaunch | None] = contextvars.ContextVar(
    "graftpunk_selenium_launch", default=None
)
# How many open blocks share the selenium proxies, as _proxy_users counts the
# nodriver one, under the same lock.
_selenium_proxy_users = 0


def _install_selenium_proxies() -> bool:
    """Bind the proxies in both launcher modules, or join the blocks already using them."""
    global _selenium_proxy_users
    modules = {}
    for name in _SELENIUM_PROXIES:
        try:
            modules[name] = importlib.import_module(name)
        except ImportError as exc:
            LOG.warning(
                "chrome_own_session_unavailable",
                reason="import",
                module=name,
                error=str(exc),
                consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
            )
            return False
    with _PROXY_LOCK:
        for name, module in modules.items():
            current = getattr(module, "subprocess", None)
            if current is not _SELENIUM_PROXIES[name] and current is not subprocess:
                LOG.warning(
                    "chrome_own_session_unavailable",
                    reason="launcher_changed",
                    module=name,
                    consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
                )
                return False
        for name, module in modules.items():
            setattr(module, "subprocess", _SELENIUM_PROXIES[name])  # noqa: B010
        _selenium_proxy_users += 1
        return True


def _remove_selenium_proxies() -> None:
    """Leave the proxies; the last block out puts the real module back."""
    global _selenium_proxy_users
    with _PROXY_LOCK:
        _selenium_proxy_users -= 1
        if _selenium_proxy_users:
            return
        for name, proxy in _SELENIUM_PROXIES.items():
            module = sys.modules.get(name)
            # Only the proxy is replaced: a name something else bound
            # meanwhile is not this block's to overwrite.
            if getattr(module, "subprocess", None) is proxy:
                setattr(module, "subprocess", subprocess)  # noqa: B010


def prepare_selenium_launch() -> bool:
    """Arm the handlers, and say whether this selenium launch gets a session of its own.

    The selenium backend's rule is the nodriver backend's (#335): a new
    session only when graftpunk's SIGTERM and SIGHUP handlers are installed
    (:func:`graftpunk.signals.termination_cleanup_installed`), since nothing
    else would end a chromedriver and a Chrome outside the process group on a
    hangup. When they are, the orphan sweep runs first, as before every
    nodriver launch, and a sweep that did something is logged. Without them
    the launch is exactly what it was before.

    Call it from the main thread: arming sets signal dispositions.
    """
    arm_termination_handlers()
    if os.name != "posix" or not signals.termination_cleanup_installed():
        return False
    report = prepare_browser_launch()
    if report:
        LOG.info(
            "chrome_orphans_cleaned",
            reaped=len(report.reaped),
            profiles_removed=len(report.profiles_removed),
        )
    return True


@contextlib.contextmanager
def selenium_in_own_session() -> Iterator[SeleniumLaunch]:
    """While open, an undetected-chromedriver launch in this context starts in a new session (#335).

    The selenium twin of :func:`chrome_in_own_session`. A terminal delivers
    Ctrl+C to its whole foreground process group, and undetected-chromedriver
    starts Chrome and chromedriver in Python's, so a Ctrl+C meant to stop a
    login recorded with ``--observe full`` killed both before the run could
    take its screenshot or read the network log through chromedriver.

    undetected-chromedriver has no option for this and builds chromedriver's
    ``Service`` itself, so Selenium's ``popen_kw`` cannot be passed. This
    rebinds the ``subprocess`` name in ``undetected_chromedriver`` (Chrome's
    ``Popen``) and in ``selenium.webdriver.common.service`` (chromedriver's)
    to an object that adds ``start_new_session=True``, and gives chromedriver
    the owner marker, for a ``Popen`` in this block's context only; one
    anywhere else, another thread's included, goes through unchanged. Blocks
    may overlap: the first in installs the objects and the last out restores
    the modules. A module whose ``subprocess`` is neither the real module nor
    that object is left alone with a warning, and the launch then starts where
    it always did. ``tests/unit/test_selenium_own_session.py`` pins the two calls
    this relies on, and :func:`check_in_own_session` checks the outcome.

    Each process is registered with the SIGTERM and SIGHUP handlers as it is
    spawned, on :attr:`SeleniumLaunch.processes`, which stays registered after
    the block until the code that quits the driver releases it
    (:func:`release_selenium_processes`). An exception leaving the block ends
    every process it spawned (:meth:`SeleniumProcesses.end`). As with
    :func:`chrome_in_own_session`, only a program with graftpunk's handlers
    installed should open it (:func:`prepare_selenium_launch`).

    Yields:
        The block's :class:`SeleniumLaunch`.
    """
    applied = os.name == "posix" and _install_selenium_proxies()
    launch = SeleniumLaunch(applied=applied)
    token = _CURRENT_SELENIUM_LAUNCH.set(launch if applied else None)
    try:
        yield launch
    except BaseException:
        launch.processes.end()
        raise
    finally:
        _CURRENT_SELENIUM_LAUNCH.reset(token)
        if applied:
            _remove_selenium_proxies()


def _shares_this_session(pid: int) -> bool | None:
    """Whether *pid* is in this process's session; None when that cannot be told."""
    getsid = getattr(os, "getsid", None)
    if getsid is None:
        return None
    try:
        return getsid(pid) == getsid(0)
    except OSError:
        return None


def check_in_own_session(pids: Iterable[int]) -> bool:
    """Check that no process in *pids* (chromedriver, Chrome) shares this session.

    The outcome check after :func:`selenium_in_own_session`, and after the
    standard Selenium path's ``popen_kw``. A Chrome that chromedriver spawned
    is in chromedriver's session rather than leading one, which is as good:
    what matters is that the terminal's Ctrl+C does not reach it. Logs
    ``chrome_not_in_own_session``, with what a Ctrl+C will cost, for each
    process that does share it; one that cannot be asked counts as fine, as in
    :func:`check_chrome_in_own_session`.

    Returns:
        False when a process is known to share the session, True otherwise.
    """
    shared = [pid for pid in pids if _shares_this_session(pid) is True]
    for pid in shared:
        LOG.warning(
            "chrome_not_in_own_session",
            pid=pid,
            consequence=CHROME_SHARES_SESSION_CONSEQUENCE,
        )
    return not shared
