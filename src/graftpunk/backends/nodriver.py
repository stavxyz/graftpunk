"""NoDriver-based browser backend using CDP-direct automation.

This backend uses nodriver for browser automation, communicating directly
with Chrome via the Chrome DevTools Protocol (CDP) without the WebDriver
binary. This eliminates a major detection vector used by anti-bot systems.

Note:
    nodriver's browser, tabs, and websocket belong to the event loop the
    browser was started on, so every call has to run there. A synchronous
    ``start()`` starts a private loop in a daemon thread and the synchronous
    methods submit their work to it. ``BrowserSession.start_async()`` starts
    the browser on the caller's loop instead, and the synchronous methods then
    submit to that loop from other threads. Before a start there is no loop:
    ``start()``, ``navigate()``, ``set_cookies()`` and ``driver`` start the
    browser first, and the other synchronous methods return their fallback
    rather than run on a loop the browser does not belong to.
    ``stop_async()`` stops the browser from inside an event loop. When the
    browser's loop is gone, or ``stop()`` is called from inside it, the stop
    ends Chrome without the loop (SIGTERM, then SIGKILL if it has not exited,
    or the subprocess handle on Windows).

Logging:
    This module uses structured logging with intentional log levels:

    - **ERROR**: Operations that fail and raise exceptions (start, navigate)
    - **WARNING**: Operations that fail silently, including property getters
      returning empty values due to errors (browser crash, CDP failure)
    - **DEBUG**: Expected conditions like browser already started/stopped

Example:
    >>> from graftpunk.backends.nodriver import NoDriverBackend
    >>> backend = NoDriverBackend(headless=False)
    >>> backend.start()
    >>> backend.navigate("https://example.com")
    >>> print(backend.page_title)
    >>> backend.stop()
"""

import asyncio
import concurrent.futures
import contextvars
import functools
import io
import math
import os
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable, Coroutine
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Self

from graftpunk._distribution import install_command
from graftpunk.backends.base import Cookie
from graftpunk.browser_launch import (
    arm_termination_handlers,
    browser_connect_failure_message,
    prepare_browser_launch,
)
from graftpunk.chrome_orphans import (
    base_browser_args,
    remove_browser_temp_profile,
    terminate_nodriver_browser,
)
from graftpunk.exceptions import BrowserError
from graftpunk.logging import get_logger, suppress_asyncio_noise
from graftpunk.signals import register_live_browser, unregister_live_browser

if TYPE_CHECKING:
    import nodriver

LOG = get_logger(__name__)

_nodriver_cookie_patched = False

# Subprocess reap timeouts. nodriver's Browser.stop() sends SIGTERM (or kill
# fallback) but never awaits proc.wait(), so the Chrome subprocess sits as a
# zombie under the live python parent. _reap_browser_process below awaits
# the subprocess to give the kernel a chance to collect its exit status.
# Module-level constants (rather than function defaults) so tests can
# monkeypatch tight timeouts.
_REAP_TERM_TIMEOUT_S: float = 3.0
_REAP_KILL_TIMEOUT_S: float = 1.0

# How long stop() waits for the private loop's thread to finish its teardown
# (cancelling leftover nodriver tasks, closing the loop). The thread is a
# daemon, so one that overruns this cannot hold the interpreter open.
_LOOP_JOIN_TIMEOUT_S: float = 5.0
_LOOP_THREAD_NAME = "graftpunk-nodriver-loop"

# How long a synchronous call waits for the browser's loop, in seconds (None
# waits without a limit). nodriver 0.50.6 bounds none of the CDP calls these
# wrap: Connection.send awaits Chrome's reply with no timeout, and its
# websocket's ping timeout is 15 minutes. Browser.get awaits the
# Page.navigate reply, which can take as long as the server takes to answer,
# so navigate gets the longer limit.
DEFAULT_CALL_TIMEOUT_S: float = 60.0
DEFAULT_NAVIGATE_TIMEOUT_S: float = 120.0

# How long a timed-out navigate() waits for Chrome to answer the
# Page.stopLoading it sends. Cancelling the wait on Page.navigate leaves
# Chrome loading the URL, and until that load ends the tab answers no
# Runtime.evaluate, so every getter would wait out its own limit.
_STOP_LOADING_TIMEOUT_S: float = 2.0

# Ending Chrome without its event loop: how often to check whether the
# signalled process has exited (bounded by _REAP_TERM_TIMEOUT_S), and how
# many times, how far apart, to try removing its temp profile.
_EXIT_POLL_S: float = 0.05
_PROFILE_REMOVE_ATTEMPTS = 3
_PROFILE_REMOVE_RETRY_S: float = 0.25

# Chrome ignores switches it does not know; this one marks which start
# launched a browser, for a start that fails before uc.start() returns it.
_LAUNCH_SWITCH = "--graftpunk-launch-id"

_IN_OWN_LOOP_MESSAGE = (
    "NoDriverBackend cannot be used from within an async context "
    "running on the browser's own event loop: the synchronous "
    "methods wait on that loop and would deadlock. In async code, "
    "await nodriver's objects (backend.driver) and stop_async() instead."
)

# A start_async() or stop_async() on the calling thread holds the backend's
# lock, on an event loop other than the running one (or there is none): it
# may be suspended on a loop that will not run again, so there is nothing a
# start can wait for.
_HELD_ON_THIS_THREAD_MESSAGE = (
    "NoDriverBackend.{method}() was called while a {holder}() on this thread "
    "is still {doing} the backend; it cannot start until that {holder}() "
    "has returned."
)

# How long the event loop of a start_async() or stop_async() holding the
# backend's lock may go without running before a call waiting for the lock
# gives up on it. Those two hold the lock across awaits, so a task left on a
# loop that stopped and never runs again keeps it for good. Elapsed time
# alone cannot tell that holder from a slow one, so the wait watches the
# holder's loop instead: while it runs, the holder can finish and is waited
# for however long it takes; once it is closed, the holder cannot, and is
# given up on at once. A loop that has stopped but is not closed (a
# hand-managed loop between run_until_complete() calls) may run again, so
# it gets this long. A synchronous start() or stop() holds the lock on a
# thread that is running, so a wait for one has no limit, as before.
_ABANDONED_HOLD_S: float = 60.0

# How long a stop that gives up on a hold another call already gave up on
# waits for that call's end of Chrome: Chrome's exit, up to
# _REAP_TERM_TIMEOUT_S plus _REAP_KILL_TIMEOUT_S, the profile's removal,
# and the private loop's join, up to _LOOP_JOIN_TIMEOUT_S, with room to spare.
_GIVEN_UP_END_WAIT_S: float = 20.0

_ABANDONED_HOLD_MESSAGE = (
    "NoDriverBackend.{method}() gave up waiting for a {holder}() on another "
    "thread that holds the backend's lock: the event loop it runs on {reason}, "
    "so it cannot finish. This backend cannot start again until it returns."
)

# The same, once that holder has returned and left the lock to the end of
# Chrome a stop that gave up on it is running.
_GIVEN_UP_END_MESSAGE = (
    "NoDriverBackend.{method}() gave up waiting for a stop on another thread "
    "to finish ending Chrome for a {holder}() it gave up on (the event loop "
    "that call ran on {reason}): that end has not finished after {wait:g} s. "
    "The {holder}() has returned, and the backend's lock is released when "
    "the end finishes."
)

# A start_async() whose event loop ran again after a call on another thread
# gave up on it and stopped the backend.
_RESUMED_AFTER_GIVE_UP_MESSAGE = (
    "NoDriverBackend.start_async() resumed after a call on another thread gave "
    "up waiting for it and stopped the backend; whatever this start launched "
    "was ended, and the backend is stopped."
)


class _PrivateLoop:
    """An event loop running in a daemon thread, owned by one backend.

    The thread runs ``asyncio.run()`` around a coroutine that waits to be told
    to stop, so the teardown is asyncio.run's own: leftover tasks (nodriver's
    websocket listener) are cancelled, async generators and the default
    executor are shut down, and the loop is closed.
    """

    def __init__(self) -> None:
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_requested: asyncio.Event | None = None
        # Set when __init__ gives up waiting, so a thread that starts late
        # returns instead of serving a loop nothing holds.
        self._abandoned = False
        self.thread = threading.Thread(target=self._main, name=_LOOP_THREAD_NAME, daemon=True)
        self.thread.start()
        if not self._ready.wait(_LOOP_JOIN_TIMEOUT_S) or self._loop is None:
            self._abandoned = True
            # A thread already waiting to be stopped is stopped; one that is
            # not there yet sees _abandoned in _serve and returns.
            self.close()
            raise RuntimeError("NoDriverBackend's event loop thread did not start")

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None  # set before __init__ returns
        return self._loop

    def _main(self) -> None:
        asyncio.run(self._serve())

    async def _serve(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_requested = asyncio.Event()
        self._ready.set()
        if self._abandoned:
            return
        await self._stop_requested.wait()

    def close(self) -> None:
        """Stop the loop and wait for its thread. Never raises.

        From the loop's own thread (``stop_async()`` awaited on it) the stop is
        only scheduled: a thread cannot join itself.
        """
        stop_requested = self._stop_requested
        if stop_requested is None:
            return
        try:
            self.loop.call_soon_threadsafe(stop_requested.set)
        except RuntimeError:
            # The loop is already closed.
            return
        if threading.current_thread() is self.thread:
            return
        self.thread.join(_LOOP_JOIN_TIMEOUT_S)
        if self.thread.is_alive():
            LOG.warning("nodriver_loop_thread_still_running", timeout_s=_LOOP_JOIN_TIMEOUT_S)


async def _reap_browser_process(proc: asyncio.subprocess.Process | None) -> None:
    """Wait for the loop's child watcher to collect Chrome's exit status.

    nodriver's Browser.stop() calls proc.terminate() (or proc.kill() as a
    fallback) and never awaits proc.wait(); only when terminate() and kill()
    both fail does it set self._process = None (nodriver 0.48.1 and 0.50.6).
    asyncio child processes must be waited for, or the kernel keeps them as
    <defunct> (zombie) entries under the parent. The watcher reaps the child
    and sets ``proc.returncode``, so this polls that (``_collected``) rather
    than await ``proc.wait()``: on CPython 3.11 and later that also waits for
    every pipe to close, which Chrome's helper processes can hold open after
    Chrome itself has exited.

    Behavior:
      1. Up to _REAP_TERM_TIMEOUT_S for the exit status: the SIGTERM path.
      2. Not collected by then: log debug, proc.kill() (SIGKILL), and up to
         _REAP_KILL_TIMEOUT_S more. SIGKILL cannot be caught or ignored.
      3. Still not collected (a kernel-level wedge): log a warning and
         return. The zombie persists until the python process exits.
      4. ProcessLookupError from proc.kill() is swallowed: Chrome exited in
         the race window between the first wait and the kill.
      5. Other OSError from proc.kill() (e.g. EPERM under sandboxed runs):
         log a distinct warning and return.

    Args:
        proc: The asyncio subprocess. None is a no-op (start() may have
            failed before subprocess spawn, or the backend was never started).
    """
    if proc is None:
        return
    if await _collected(proc, _REAP_TERM_TIMEOUT_S):
        return
    LOG.debug("chrome_sigterm_timeout_escalating_to_sigkill", pid=proc.pid)
    try:
        proc.kill()
    except ProcessLookupError:
        return
    except OSError as exc:
        LOG.warning(
            "chrome_kill_failed",
            pid=proc.pid,
            errno=exc.errno,
            error=str(exc),
        )
        return
    if not await _collected(proc, _REAP_KILL_TIMEOUT_S):
        LOG.warning(
            "chrome_failed_to_exit_after_sigkill",
            pid=proc.pid,
            returncode=proc.returncode,
        )


async def _collected(proc: asyncio.subprocess.Process, timeout_s: float) -> bool:
    """Whether the loop's child watcher collects *proc*'s exit status within *timeout_s*."""
    deadline = time.monotonic() + timeout_s
    while proc.returncode is None:
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_EXIT_POLL_S)
    return True


def _signals_available() -> bool:
    """Whether Chrome can be ended with SIGTERM, which ``terminate_nodriver_browser`` needs."""
    return os.name == "posix"


def _nodriver_registry() -> set[Any] | None:
    """nodriver's set of live browsers, which its ``atexit`` handler stops, or None."""
    try:
        from nodriver.core.util import get_registered_instances

        return get_registered_instances()
    except Exception:  # noqa: BLE001 - best-effort: a missing registry only costs the cleanup
        LOG.debug("nodriver_registry_unavailable", exc_info=True)
        return None


def _terminate_through_handle(proc: Any) -> bool:
    """End Chrome through its subprocess handle, where there are no POSIX signals. Never raises."""
    if proc is None:
        return False
    try:
        proc.terminate()
    except ProcessLookupError:
        return False
    except Exception as exc:  # noqa: BLE001 - best-effort: this runs on cleanup paths
        LOG.debug("chrome_process_terminate_failed", error=str(exc))
        return False
    return True


def _already_reaped(proc: Any) -> bool:
    """Whether *proc*'s exit status was collected, which frees its pid for reuse."""
    return isinstance(getattr(proc, "returncode", None), int)


def _exited_unreaped(pid: int) -> bool | None:
    """Whether child *pid* has exited, read without reaping it; None when this cannot tell.

    ``waitid`` with ``WNOWAIT`` leaves the exit status for asyncio's child
    watcher. On Linux that watcher reaps from a callback on the browser's
    loop, which is blocked or closed whenever Chrome is ended without it, so
    an exited Chrome would keep answering ``kill(pid, 0)``. CPython has no
    ``os.waitid`` on macOS, where asyncio reaps from a thread instead.
    """
    waitid = getattr(os, "waitid", None)
    if waitid is None:
        return None
    try:
        result = waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except OSError:
        # ChildProcessError: not this process's child, or already reaped.
        return None
    return result is not None


def _has_exited(proc: Any) -> bool:
    """Whether the Chrome behind *proc* is gone, or there is no way to tell. Never raises.

    An exited child not yet reaped counts as gone where ``waitid`` can tell
    (``_exited_unreaped``). Elsewhere it answers ``kill(pid, 0)`` until
    asyncio's child watcher reaps it, and the caller's wait is bounded for
    when that never happens.
    """
    if _already_reaped(proc):
        return True
    pid = getattr(proc, "pid", None)
    if not isinstance(pid, int):
        return True
    if _signals_available():
        exited = _exited_unreaped(pid)
        if exited is not None:
            return exited
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except OSError:
            # EPERM: the pid exists and belongs to someone else now.
            return True
        return False
    popen = getattr(getattr(proc, "_transport", None), "_proc", None)
    if popen is None:
        return True
    try:
        return popen.poll() is not None
    except Exception:  # noqa: BLE001 - best-effort: no answer means stop waiting
        return True


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The calling thread's running event loop, or None."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _close_transport(proc: Any) -> None:
    """Close the asyncio subprocess transport behind *proc*, now or on its loop. Never raises.

    nodriver never closes it, and Chrome's helper processes can hold its
    pipes open after Chrome exits, so it outlives the loop unless closed; a
    transport collected after its loop closed makes the interpreter print
    ``Exception ignored in BaseSubprocessTransport.__del__ ... Event loop is
    closed``.

    The close happens on the loop's thread, since asyncio transports are
    not thread-safe, and only after that loop's child watcher has collected
    Chrome's exit status: before that, the close calls ``Popen.poll()``,
    which reaps Chrome ahead of the watcher, and the watcher then logs "exit
    status already read" (on Linux, where the watcher runs on the loop, the
    process then reports exit status 255). ``Process.wait()`` is no signal
    for that, since it also waits for the helpers to release the pipes, so
    the close is scheduled on the loop and checks the status there
    (``_ScheduledClose``). That includes a loop that has stopped but
    is not closed (a hand-managed loop between ``run_until_complete()``
    calls): its watcher still runs once the loop runs again.

    A scheduled close runs only if its loop runs again. A stopped loop that
    is closed without running again drops the scheduled close instead, and
    the close then happens as it is dropped (``_ScheduledClose.__del__``),
    without the loop (``_close_transport_now``). Only on the loop's own
    thread with the status already collected, or on a loop already closed,
    does this close at once.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    loop = getattr(transport, "_loop", None)
    if (
        isinstance(loop, asyncio.AbstractEventLoop)
        and not loop.is_closed()
        and (_running_loop() is not loop or not _already_reaped(proc))
    ):
        try:
            loop.call_soon_threadsafe(_ScheduledClose(proc, transport, loop).run)
            return
        except RuntimeError:
            # The loop closed after the check.
            pass
    _close_transport_now(transport)


# Checks of the exit status a scheduled close makes on consecutive loop
# iterations before it falls back to one every _EXIT_POLL_S: the watcher's
# callback is usually queued already, and a loop shutting down runs only a
# few more iterations.
_CLOSE_QUICK_CHECKS = 20


class _ScheduledClose:
    """A transport close scheduled on its loop, run once that loop has collected Chrome's exit.

    Past its deadline it closes anyway: a watcher that has not collected the
    status by then is not going to, and an open transport is the worse leak.
    The first run sets the deadline (``_REAP_TERM_TIMEOUT_S`` plus
    ``_REAP_KILL_TIMEOUT_S`` from then), so a loop that was blocked or
    stopped after the close was scheduled still gives its watcher that long.

    Only the loop's pending callback refers to it. A loop closed before
    running it drops that callback, and with it this object, while the
    transport is still referenced here: ``__del__`` then closes the
    transport without the loop (``_close_transport_now``), before the
    transport's own ``__del__`` would try a close on the closed loop and
    print ``Exception ignored in ... Event loop is closed``.
    """

    def __init__(self, proc: Any, transport: Any, loop: asyncio.AbstractEventLoop) -> None:
        self._proc = proc
        self._transport = transport
        self._loop = loop
        self._deadline: float | None = None
        self._checks = 0
        self._done = False

    def run(self) -> None:
        """On the transport's loop: close it now if the exit was collected, else check again."""
        now = time.monotonic()
        if self._deadline is None:
            self._deadline = now + _REAP_TERM_TIMEOUT_S + _REAP_KILL_TIMEOUT_S
        if _already_reaped(self._proc) or now >= self._deadline:
            self._done = True
            _close_transport_now(self._transport)
            return
        delay = 0.0 if self._checks < _CLOSE_QUICK_CHECKS else _EXIT_POLL_S
        self._checks += 1
        self._loop.call_later(delay, self.run)

    def __del__(self) -> None:
        # Only a loop that closed can drop a pending close; a loop still open
        # would run it, and closing from here would race it.
        try:
            if not self._done and self._loop.is_closed():
                _close_transport_now(self._transport)
        except Exception:  # noqa: BLE001, S110 - a finalizer must not raise, even at shutdown
            pass


def _close_transport_now(transport: Any) -> None:
    """``transport.close()``, finished by hand when its loop is closed. Never raises.

    On a closed loop ``close()`` marks the transport closed and then raises
    at its first pipe, since a pipe transport's close schedules its last
    step on the loop. That leaves every pipe's file descriptor open and the
    child's exit status unread, and each warns when collected (``unclosed
    transport``, ``subprocess ... is still running``). So on a closed loop
    the pipes are closed here directly, and the exit status is read with
    ``Popen.poll()``: the loop's child watcher cannot collect it any more on
    Linux, where it runs on the loop, and on macOS its thread has already
    collected it once Chrome is known to have exited. A child still running
    is left running.
    """
    try:
        transport.close()
    except Exception:  # noqa: BLE001 - best-effort: a closed loop makes this raise
        LOG.debug("chrome_transport_close_failed", exc_info=True)
    loop = getattr(transport, "_loop", None)
    if isinstance(loop, asyncio.AbstractEventLoop) and loop.is_closed():
        _release_transport_of_closed_loop(transport)


def _release_transport_of_closed_loop(transport: Any) -> None:
    """Close the pipes of a subprocess transport whose loop is closed, and poll its child."""
    pipes = getattr(transport, "_pipes", None)
    for protocol in list(pipes.values()) if isinstance(pipes, dict) else []:
        pipe_transport: Any = getattr(protocol, "pipe", None)
        raw = getattr(pipe_transport, "_pipe", None)
        if raw is None:
            continue
        try:
            raw.close()
        except OSError:
            LOG.debug("chrome_transport_pipe_close_failed", exc_info=True)
        # What the pipe transport's own close sets last, on the loop; its
        # __del__ warns while it is set.
        pipe_transport._pipe = None
    popen = getattr(transport, "_proc", None)
    if popen is not None:
        try:
            popen.poll()
        except Exception:  # noqa: BLE001 - best-effort: this runs on cleanup paths
            LOG.debug("chrome_process_poll_failed", exc_info=True)


def _wait_for_exit(proc: Any, timeout_s: float) -> bool:
    """Poll until the Chrome behind *proc* has exited, for up to *timeout_s*. Whether it did."""
    deadline = time.monotonic() + timeout_s
    while not _has_exited(proc):
        if time.monotonic() >= deadline:
            return False
        time.sleep(_EXIT_POLL_S)
    return True


def _kill(proc: Any) -> bool:
    """SIGKILL the Chrome behind *proc*, or kill it through its handle off POSIX. Never raises.

    Sent only when the check made just before (``kill(pid, 0)``, or the
    ``waitid`` peek on Linux) found Chrome still running. This code never
    reaps Chrome, but asyncio's child watcher can: its thread on macOS, or
    the loop on Linux while the loop runs. A Chrome that exited and was
    reaped between that check and this signal, its pid already reused,
    would have the SIGKILL go to another process. Nothing rules that out, so
    this is best effort; the window is one debug log call long.
    Not ``proc.kill()`` on POSIX: ``Popen.send_signal`` polls first, and the
    poll reaps the child the loop's watcher is waiting for.
    """
    try:
        if _signals_available():
            os.kill(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except Exception as exc:  # noqa: BLE001 - best-effort: this runs on cleanup paths
        LOG.debug("chrome_kill_failed", pid=getattr(proc, "pid", None), error=str(exc))
        return False
    return True


def _end_browser_without_loop(browser: Any) -> None:
    """End *browser*'s Chrome and remove its temp profile without its event loop. Never raises.

    SIGTERM on POSIX (the signal handler's sequence), the subprocess handle
    elsewhere, then up to ``_REAP_TERM_TIMEOUT_S`` for Chrome to exit, then
    SIGKILL and up to ``_REAP_KILL_TIMEOUT_S`` more, as the stop through the
    loop does. A Chrome whose exit status was already collected is not
    signalled, since its pid may belong to another process by now. The
    profile is removed, with retries, only once Chrome has exited: removing
    it while Chrome still writes to it leaves the directory behind. There is
    no subprocess reap: that is the loop's child watcher's job.
    """
    proc = getattr(browser, "_process", None)
    if _already_reaped(proc):
        ended = False
    elif _signals_available():
        ended = terminate_nodriver_browser(browser)
    else:
        ended = _terminate_through_handle(proc)
    if ended and not _wait_for_exit(proc, _REAP_TERM_TIMEOUT_S):
        LOG.debug("chrome_sigterm_timeout_escalating_to_sigkill", pid=getattr(proc, "pid", None))
        if _kill(proc):
            _wait_for_exit(proc, _REAP_KILL_TIMEOUT_S)
    if not _has_exited(proc):
        LOG.warning(
            "chrome_still_running_profile_kept",
            pid=getattr(proc, "pid", None),
            hint="Chrome did not exit, so its temp profile was left in place; "
            "a later launch's stale sweep removes it once no browser uses it.",
        )
        return
    _close_transport(proc)
    remove_browser_temp_profile(
        browser, attempts=_PROFILE_REMOVE_ATTEMPTS, retry_delay_s=_PROFILE_REMOVE_RETRY_S
    )


def _launched_with(browser: Any, launch_arg: str) -> bool:
    """Whether *browser* is the one a ``uc.start()`` given *launch_arg* created.

    Every start adds its own ``--graftpunk-launch-id=<random>`` switch to
    ``browser_args``, and nodriver's ``Config`` keeps those arguments, so the
    switch tells this start's browser from one another thread started at the
    same time. A browser whose arguments cannot be read is left alone: a
    leaked Chrome is the lesser harm next to ending another caller's.
    """
    args = getattr(getattr(browser, "config", None), "_browser_args", None)
    return isinstance(args, list) and launch_arg in args


def _take_unreturned_browsers(
    registry: set[Any] | None, before: set[Any], launch_arg: str
) -> list[Any]:
    """Remove from the registry, and return, the browsers a failed ``uc.start()`` never returned.

    nodriver's ``Browser.start()`` spawns Chrome and adds it to the registry,
    then connects to it and attaches, and only then returns. A start that
    raised in between ("Failed to connect to browser" is raised there) or was
    interrupted (Ctrl-C) leaves a running Chrome that no handle outside the
    registry points at. Runs on the loop that owns the registry; the caller
    ends what it returns.
    """
    if registry is None:
        return []
    taken = [b for b in set(registry) - before if _launched_with(b, launch_arg)]
    for browser in taken:
        registry.discard(browser)
    return taken


def _end_browsers_without_loop(browsers: list[Any]) -> None:
    """``_end_browser_without_loop`` for each of *browsers*. Never raises."""
    for browser in browsers:
        _end_browser_without_loop(browser)


def _start_daemon_thread(target: Callable[[], None]) -> None:
    """Start *target* on a daemon thread of its own."""
    threading.Thread(target=target, name="graftpunk-nodriver-end", daemon=True).start()


def _start_off_loop(work: Callable[[], None]) -> "asyncio.Future[None]":
    """Start *work* off the running loop now, and return a future for its outcome.

    It is handed to a thread before this returns, not in a later iteration
    of the loop, so it runs even if the loop stops in this one. The loop's
    default executor runs it; when that cannot (shut down, or the
    interpreter is finalising), a thread of its own does; and when no
    thread can be started either, it runs here, blocking the loop.
    """
    loop = asyncio.get_running_loop()
    call = functools.partial(contextvars.copy_context().run, work)
    try:
        return loop.run_in_executor(None, call)
    except RuntimeError:
        return _start_on_own_thread(loop, call)


def _start_on_own_thread(
    loop: asyncio.AbstractEventLoop, call: Callable[[], None]
) -> "asyncio.Future[None]":
    """``_start_off_loop()`` without the loop's executor: a thread of its own, or else here."""
    future: asyncio.Future[None] = loop.create_future()

    def settle(error: BaseException | None) -> None:
        if future.done():
            return
        if error is None:
            future.set_result(None)
        else:
            future.set_exception(error)

    def run() -> None:
        error: BaseException | None = None
        try:
            call()
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting coroutine
            error = exc
        # A closed loop raises RuntimeError: nothing awaits the outcome then.
        with suppress(RuntimeError):
            loop.call_soon_threadsafe(settle, error)

    try:
        _start_daemon_thread(run)
    except RuntimeError:
        call()
        future.set_result(None)
    return future


async def _finish_on_worker_thread(work: Callable[[], None]) -> None:
    """Run *work* on a worker thread and wait for it, even through a cancellation.

    For ending Chrome from a coroutine: the end polls and sleeps for up to a
    few seconds, which would block the event loop, and abandoning it halfway
    would leave Chrome running or its profile behind. A cancellation that
    arrives meanwhile is raised once *work* has finished. The work starts
    before the first await (``_start_off_loop()``), so a loop that stops in
    this iteration does not keep it from running.
    """
    task = _start_off_loop(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    task.result()


class _Hold(NamedTuple):
    """One hold of a backend's lock: its number, thread, method, and event loop.

    *loop* is the running loop of a ``start_async()`` or ``stop_async()``,
    and None for a synchronous ``start()`` or ``stop()``. The thread object
    rather than its ident: a holder abandoned along with its thread leaves
    an ident that a new thread can be given.
    """

    number: int
    owner: threading.Thread
    method: str
    loop: asyncio.AbstractEventLoop | None


class _Abandonment:
    """A hold that waiters gave up on, and the end of Chrome the first stop among them runs.

    A start that gives up never ends Chrome, so the first stop to give up on
    the hold claims the end (*ender_claimed*), whether or not a start gave
    up before it. Another stop that gives up on the hold waits for *ended*
    (``_GIVEN_UP_END_WAIT_S`` at most), so it does not return while the
    first is still ending Chrome, and a start waits for it too, so it does
    not launch a Chrome that end then forgets. The lock stays held for the
    end: a holder that returns meanwhile leaves it held (*release_on_end*),
    and the end releases it once done.
    """

    def __init__(self, hold: int, holder: str, reason: str) -> None:
        self.hold = hold
        self.holder = holder
        self.reason = reason
        self.ended = threading.Event()
        self.ender_claimed = False
        self.release_on_end = False


class _LockWait:
    """One caller's wait for a backend's lock, and when it gives up on the holder.

    It gives up on a ``start_async()`` or ``stop_async()`` whose event loop
    is closed, or has not run for ``_ABANDONED_HOLD_S`` straight, and at once
    on a hold an earlier waiter gave up on. A holder whose loop runs is
    waited for, however long it takes, and so is a synchronous holder, whose
    thread is running the start or stop. Whether a stopped loop ran between
    two polls is told by a callback this wait leaves on it, so a loop run in
    bursts shorter than the poll counts as running. A holder on the waiter's
    own running loop is waited for, since it runs whenever the waiter polls,
    until it has returned and left the lock to a stop's end of Chrome; the
    wait is then for that end, and is bounded as on any other loop.

    *ends* is whether the waiter is a stop, which ends Chrome once it gives
    up. A start that gives up waits first for a stop's end of Chrome in
    progress (``_GIVEN_UP_END_WAIT_S`` at most), after which the lock is
    free unless the holder has still not returned.
    """

    def __init__(self, backend: "NoDriverBackend", *, ends: bool) -> None:
        self._backend = backend
        self._ends = ends
        self._hold: int | None = None
        self._idle_since = 0.0
        self._end_seen: float | None = None
        # The holder's loop and a list its callback appends to once it runs.
        self._beat: tuple[asyncio.AbstractEventLoop, list[bool]] | None = None

    def gave_up_on(self) -> "tuple[_Abandonment, bool] | None":
        """The hold this wait gives up on now, and whether it is the one to end Chrome, or None."""
        backend = self._backend
        holding = backend._holding
        abandoned = backend._abandoned
        if abandoned is not None and (holding is None or abandoned.hold != holding.number):
            abandoned = None
        # A holder on this wait's own running loop runs whenever the wait
        # polls, so it is waited for, until it has returned and left the lock
        # to a stop's end of Chrome: that end is what is waited for then.
        if (
            holding is None
            or holding.loop is None
            or (
                holding.loop is _running_loop()
                and not (abandoned is not None and abandoned.release_on_end)
            )
        ):
            self._hold = None
            return None
        if abandoned is not None:
            reason = abandoned.reason
        else:
            ran = self._loop_ran(holding.loop)
            now = time.monotonic()
            if ran is None:
                reason = "is closed"
            elif ran or holding.number != self._hold:
                self._hold = holding.number
                self._idle_since = now
                return None
            elif now - self._idle_since < _ABANDONED_HOLD_S:
                return None
            else:
                reason = f"has not run for {_ABANDONED_HOLD_S:g} s"
        given_up = backend._give_up_on(holding, reason, ends=self._ends)
        if given_up is not None and not self._ends and self._end_running(given_up[0]):
            return None
        return given_up

    def _end_running(self, abandonment: _Abandonment) -> bool:
        """Whether a stop is ending Chrome for *abandonment*, and this start still waits for it."""
        if not abandonment.ender_claimed:
            return False
        if abandonment.ended.is_set():
            # Done. If its holder had returned, the end has released the
            # lock or releases it right after setting *ended*, so the next
            # poll takes it, rather than this start raising about an end
            # that finished as it looked.
            return abandonment.release_on_end
        now = time.monotonic()
        if self._end_seen is None:
            self._end_seen = now
        return now - self._end_seen < _GIVEN_UP_END_WAIT_S

    def _loop_ran(self, loop: asyncio.AbstractEventLoop) -> bool | None:
        """Whether *loop* is running or ran since this wait last looked; None once it is closed."""
        if loop.is_closed():
            return None
        if loop.is_running():
            self._beat = None
            return True
        beat = self._beat
        ran = beat is not None and beat[0] is loop and bool(beat[1])
        if beat is None or beat[0] is not loop or ran:
            flag: list[bool] = []
            try:
                loop.call_soon_threadsafe(flag.append, True)
            except RuntimeError:
                # The loop closed after the check.
                return None
            self._beat = (loop, flag)
        return ran


def _patch_nodriver_cookie_parsing() -> None:
    """Patch nodriver's Cookie.from_json to handle missing 'sameParty' field.

    Chrome removed the 'sameParty' field from CDP Network.Cookie responses,
    but nodriver 0.48.1 still does a hard key lookup that raises KeyError.
    This patches Cookie.from_json to use .get() with a default instead.

    See: https://github.com/nicefail/nodriver/issues/new (issues restricted)
    Idempotent — safe to call multiple times.
    """
    global _nodriver_cookie_patched  # noqa: PLW0603
    if _nodriver_cookie_patched:
        return

    try:
        from nodriver.cdp.network import Cookie
    except ImportError:
        return

    _original_from_json = Cookie.from_json.__func__

    @classmethod  # type: ignore[misc]
    def _patched_from_json(cls, json):  # type: ignore[no-untyped-def]  # noqa: N805, ANN001
        json.setdefault("sameParty", False)
        return _original_from_json(cls, json)

    Cookie.from_json = _patched_from_json  # type: ignore[assignment]
    _nodriver_cookie_patched = True
    LOG.debug("nodriver_cookie_sameparty_patched")


def _cookie_from_cdp(cdp_cookie: "nodriver.cdp.network.Cookie") -> Cookie:
    """Convert a CDP ``Network.Cookie`` to the backend's Cookie dict."""
    cookie: Cookie = {
        "name": cdp_cookie.name,
        "value": cdp_cookie.value,
        "domain": cdp_cookie.domain,
        "path": cdp_cookie.path,
        "secure": cdp_cookie.secure,
        "httponly": cdp_cookie.http_only,
    }
    if cdp_cookie.same_site is not None:
        cookie["sameSite"] = cdp_cookie.same_site.value
    # CDP reports a session cookie with session=True and expires=-1.
    if not cdp_cookie.session and cdp_cookie.expires is not None:
        cookie["expires"] = cdp_cookie.expires
    return cookie


def _cookie_param_from_dict(
    cookie: Cookie,
) -> tuple["nodriver.cdp.network.CookieParam | None", str]:
    """Build a CDP ``CookieParam`` from a Cookie dict.

    Returns:
        ``(param, "")``, or ``(None, reason)`` when CDP cannot set the cookie.
        The reason never contains the cookie's value.
    """
    import nodriver.cdp.network as cdp_network

    if not cookie.get("name"):
        return None, "no name"
    if cookie.get("value") is None:
        return None, "no value"
    if not cookie.get("domain"):
        # Storage.setCookies needs a domain or a URL to place the cookie, and
        # the Cookie dict carries no URL.
        return None, "no domain"
    same_site = None
    if (raw_same_site := cookie.get("sameSite")) is not None:
        by_name = {s.value.lower(): s for s in cdp_network.CookieSameSite}
        same_site = by_name.get(str(raw_same_site).lower())
        if same_site is None:
            return None, f"sameSite {raw_same_site!r} is not Strict, Lax, or None"
    expires = None
    if (raw_expires := cookie.get("expires")) is not None:
        try:
            seconds = float(raw_expires)
        except (TypeError, ValueError):
            return None, "expires is not a number"
        # nan and inf parse, but Storage.setCookies rejects the whole batch.
        if not math.isfinite(seconds):
            return None, "expires is not a finite number"
        expires = cdp_network.TimeSinceEpoch(seconds)
    return (
        cdp_network.CookieParam(
            name=cookie["name"],
            value=cookie["value"],
            domain=cookie["domain"],
            path=_cookie_path(cookie),
            secure=cookie.get("secure"),
            http_only=cookie.get("httponly"),
            same_site=same_site,
            expires=expires,
        ),
        "",
    )


def _cookie_path(cookie: Cookie) -> str:
    """The path a cookie is set at: its 'path', or '/' when that is missing or empty."""
    return cookie.get("path") or "/"


def _jar_has(jar: "list[nodriver.cdp.network.Cookie]", cookie: Cookie) -> bool:
    """Whether ``jar`` holds ``cookie``: its name, value, domain, and the path it was set at.

    The value is compared because Chrome can reject an overwrite without an
    error and keep the old cookie under the same name. Domains compare without
    a leading dot, which marks a domain cookie in the jar and may or may not be
    present in the requested dict.
    """
    domain = cookie.get("domain", "").lstrip(".")
    path = _cookie_path(cookie)
    return any(
        c.name == cookie["name"]
        and c.value == cookie["value"]
        and c.domain.lstrip(".") == domain
        and c.path == path
        for c in jar
    )


@contextmanager
def _capture_nodriver_stdout():
    """Log, at debug level, whatever nodriver prints to stdout inside the block.

    nodriver's printing cleanup is ``deconstruct_browser()`` ("successfully
    removed temp profile"), its ``atexit`` handler, which the stop paths keep
    out of by deregistering the browser; ``Browser.stop()`` prints nothing in
    0.48.1 or 0.50.6. This wraps that one synchronous call in case a later
    version prints there.

    Not thread-safe: it replaces ``sys.stdout`` for the whole process, so it
    only ever wraps a synchronous call, never an await, a join, or a wait.
    """
    old_stdout = sys.stdout
    captured = io.StringIO()
    sys.stdout = captured
    try:
        yield
    finally:
        sys.stdout = old_stdout
        suppressed = captured.getvalue()
        if suppressed:
            LOG.debug("suppressed_nodriver_cleanup_output", output=suppressed.strip())


class NoDriverBackend:
    """Browser backend using nodriver (CDP-direct).

    NoDriver communicates with Chrome via CDP without the WebDriver binary,
    avoiding the most common detection signals. This backend wraps nodriver's
    async API with a synchronous interface for compatibility with graftpunk's
    BrowserSession.

    Attributes:
        BACKEND_TYPE: Identifier for this backend type ("nodriver").

    Event loops:
        nodriver objects only work on the event loop that created them, so the
        backend records that loop in ``_loop`` and runs every synchronous call
        on it. Only a synchronous ``start()`` creates a loop: a private one in
        a daemon thread named ``graftpunk-nodriver-loop``, which ``stop()``
        stops, joining the thread. ``BrowserSession.start_async()`` starts the
        browser on the caller's loop instead, which the backend uses but never
        stops. A synchronous method works from any thread, including one
        running a different event loop (a FastAPI handler, say), except from
        the browser's own loop, where waiting on the loop would deadlock.
        There ``start()`` raises ``RuntimeError``, ``navigate()`` raises
        ``BrowserError``, and the other synchronous methods log a warning and
        return their fallback (``''``, ``[]``, ``0``, ``False``); async code
        awaits nodriver's own objects (``driver``) and ``stop_async()``
        instead. ``stop()`` is the exception: inside the browser's loop it
        ends Chrome without the loop, as below. Before a start, and after
        unpickling, the backend is stopped: ``start()``, ``navigate()``,
        ``set_cookies()`` and ``driver`` start it, and the read methods return
        their fallback until then.

        ``stop()`` and ``stop_async()`` end Chrome without the loop when they
        cannot use it: when the loop has stopped or closed (a caller's loop
        that finished), and when ``stop()`` is called from inside the
        browser's own loop, which also logs ``nodriver_backend_stop_in_own_loop``
        at warning level. That path sends SIGTERM (on Windows it terminates
        the subprocess through its handle), leaves nodriver's registry, waits
        up to ``_REAP_TERM_TIMEOUT_S`` for Chrome to exit, sends SIGKILL and
        waits up to ``_REAP_KILL_TIMEOUT_S`` more if it has not, and removes
        the temp profile once Chrome has exited (a Chrome still running keeps
        it, with a ``chrome_still_running_profile_kept`` warning). It never
        reaps the subprocess, which is the loop's child watcher's job, and it
        does not signal a Chrome whose exit status was already collected. A
        start or stop that fails partway or is interrupted (Ctrl-C, or a
        cancelled task) ends its Chrome the same way before the exception
        propagates; from a coroutine (``start_async()``, ``stop_async()``)
        that end runs on a worker thread, so the caller's loop keeps running.
        Chrome's subprocess transport is closed on its loop's thread once the
        loop has collected Chrome's exit status, or after a bounded wait. A
        loop closed right after the stop without running again never runs
        that close, so the close happens as the closing loop drops it,
        without the loop: the transport's pipes are closed directly and
        Chrome's exit status is read (``_close_transport``).

        ``start()``, ``start_async()``, ``stop()``, and ``stop_async()`` hold
        a per-backend lock, so two threads starting one backend launch one
        Chrome, whichever of the two starts each uses, and a stop racing
        another thread's start stops the Chrome that start launched. The
        async methods wait for it by polling, so their loop keeps running. A
        ``stop()`` inside the browser's own loop does not take the lock (a
        start may hold it while waiting on that loop). The lock is not
        reentrant. A coroutine whose own loop holds it through another task's
        ``start_async()`` (or, for a ``start_async()``, a ``stop_async()``)
        waits for that task, which can finish meanwhile: a second
        ``start_async()`` then finds the backend started, and a
        ``stop_async()`` stops the browser the start launched. Otherwise no
        call waits for the lock while its own thread holds it: while a
        ``stop_async()`` on a thread holds it (paused at an await, or
        suspended on a loop that stopped), a ``stop()`` or ``stop_async()``
        on that thread stops the browser itself without the lock (through
        the browser's loop while that loop runs, and without it otherwise),
        and a ``start()`` there raises ``RuntimeError``, as does a
        ``start_async()`` on another loop of that thread. The paused
        ``stop_async()``, if it resumes, finds the backend stopped. If that
        ``stop_async()`` is already ending Chrome without the loop (it was
        cancelled, or the browser's loop had stopped), the call finds nothing
        left to stop and returns before Chrome has exited (up to about 4 s
        before), and the paused ``stop_async()`` finishes ending Chrome.
        Either way the private loop is closed only after Chrome has exited or
        the stop has given up on it, since on Linux its child watcher
        collects Chrome's exit status. While a ``start_async()`` on a thread
        holds the lock, a ``start()`` there raises, and a ``stop()`` there,
        or a ``stop_async()`` on another loop of that thread, finds the
        backend not started yet and returns; the start then finishes and
        leaves the backend started.

        A ``start_async()`` or ``stop_async()`` holds the lock across awaits,
        so one whose task is left on a loop that stopped and never runs again
        would hold it for good. A call on another thread therefore watches
        the holder's event loop while it waits. While that loop runs, the
        holder can finish, and the call waits however long it takes; a
        ``start_async()`` whose Chrome never answers its first CDP call is
        waited for without a limit, as a synchronous ``start()`` is. Once
        the loop is closed, or has not run for ``_ABANDONED_HOLD_S`` (60 s)
        straight, the call gives up on the holder and logs
        ``nodriver_backend_lock_holder_abandoned``: a stop then ends Chrome
        without the browser's loop, as above, and a start raises
        ``RuntimeError`` naming the holder. Later calls give up on that same
        hold at once; a later stop also waits for the first one's end of
        Chrome before it returns. If the holder's loop runs again after all,
        a ``start_async()`` ends whatever it launched and raises rather than
        mark the backend started, and a ``stop_async()`` finds the backend
        stopped. A synchronous ``start()`` or ``stop()`` holding the lock
        is waited for without a limit, since its thread is doing the work.
        ``start_async()`` on a backend already started on a running loop, or
        on the current one, does nothing; one whose loop has finished is
        ended and replaced.

    Timeouts:
        nodriver bounds none of the CDP calls the synchronous methods wait on,
        so a Chrome that stops answering without closing its websocket would
        block the caller indefinitely. ``navigate()`` waits up to
        ``navigate_timeout`` (default 120 s) and the other synchronous methods
        up to ``call_timeout`` (default 60 s). When the limit passes,
        graftpunk stops waiting and cancels the call on the browser's loop,
        and the method fails as it does for any other CDP failure:
        ``navigate()`` raises ``BrowserError``, and the others log their usual
        warning (its ``error`` names the method and the limit) and return
        their fallback. The loop keeps running, so the next call works.

        Cancelling ends graftpunk's wait, not a command Chrome has already
        received. A navigation Chrome is still loading would keep the tab
        from answering any other call until the load ends, and then put the
        tab on that URL, so a ``navigate()`` that times out also sends Chrome
        ``Page.stopLoading`` (waiting up to 2 s for its answer) before it
        raises. The tab then stays on the page it showed before and answers
        at once. A Chrome too frozen to answer in those 2 s gets the command
        when it resumes. When the stop fails, or gets no answer within its
        2 s, graftpunk logs ``nodriver_backend_stop_loading_failed``.

        ``None`` turns a limit off. ``start()``, ``stop()``, ``stop_async()``,
        and async code awaiting nodriver's objects directly are not limited
        by these; the lock wait above has its own limit.

    Note:
        Default headless=False because nodriver is more detectable in headless
        mode. For best anti-detection, run in visible (headed) mode.

    Design Note:
        This class maintains both ``_started`` and ``_browser`` state. While
        ``_browser is not None`` could theoretically replace ``_started``,
        we keep both for defensive programming: ``is_running`` checks both
        to handle edge cases where the browser crashes (``_browser`` becomes
        invalid but ``_started`` is still True). The ``stop()`` method
        always resets both in a finally block to ensure consistent state.
    """

    BACKEND_TYPE: str = "nodriver"

    # Expected error patterns during browser cleanup - these indicate normal
    # shutdown race conditions and should be logged at DEBUG level
    _EXPECTED_STOP_PATTERNS: frozenset[str] = frozenset(
        [
            "cannot schedule",
            "browser is already closed",
            "no such process",
            "event loop is closed",
            "target closed",
            # _run_async's message when another thread's stop cancelled the call.
            "call was cancelled",
        ]
    )

    def _is_expected_stop_error(self, error_str: str) -> bool:
        """Check if error message indicates expected cleanup behavior."""
        error_lower = error_str.lower()
        return any(pattern in error_lower for pattern in self._EXPECTED_STOP_PATTERNS)

    def _handle_stop_error(self, exc: RuntimeError | OSError) -> None:
        """Classify and log a browser stop error."""
        if self._is_expected_stop_error(str(exc)):
            LOG.debug("nodriver_backend_stop_expected", error=str(exc))
        else:
            LOG.warning("nodriver_backend_stop_unexpected", error=str(exc))

    def _reset_state(self) -> None:
        """Reset browser state after stop, and leave the signal registry.

        Both stop paths call this from a ``finally``, so the registry entry
        outlives the risky part of a stop: a stop that raises still leaves a
        handle the termination handler can use, and a stop that succeeds takes
        the entry with it (#96).

        It leaves the event loop alone: the signal handler calls it too, and a
        handler cannot wait for a loop thread to finish. The stop paths release
        the loop themselves with ``_release_loop()``.
        """
        unregister_live_browser(self)
        self._browser = None
        self._page = None
        self._started = False
        LOG.info("nodriver_backend_stopped")

    def __init__(
        self,
        headless: bool = False,
        profile_dir: Path | None = None,
        default_timeout: int = 15,
        call_timeout: float | None = DEFAULT_CALL_TIMEOUT_S,
        navigate_timeout: float | None = DEFAULT_NAVIGATE_TIMEOUT_S,
        **options: Any,
    ) -> None:
        """Initialize the NoDriver backend.

        Args:
            headless: Run browser in headless mode. Default False for better
                anti-detection (nodriver is more detectable when headless).
            profile_dir: Directory for browser profile persistence. If None,
                nodriver uses a temporary profile that is auto-deleted on exit.
                Note: When profile_dir is None, nodriver uses a temporary profile
                that is deleted on exit. In contrast, SeleniumBackend's stealth
                mode defaults to a persistent profile at
                ``~/.config/graftpunk/chrome_profile``.
            default_timeout: Default timeout for operations in seconds.
                Note: Currently stored for serialization but not actively
                enforced for all operations. Reserved for future use.
            call_timeout: Seconds a synchronous getter or cookie call
                (``current_url``, ``page_title``, ``page_source``,
                ``get_cookies()``, ``set_cookies()``, ``delete_all_cookies()``,
                ``get_user_agent()``) waits for the browser before it cancels
                the call and returns its fallback with a warning. None waits
                without a limit. Default 60.
            navigate_timeout: Seconds ``navigate()`` waits before it stops
                waiting, tells Chrome to stop loading the URL, and raises
                ``BrowserError``; the tab stays on the page it showed
                before. None waits without a limit. Default 120.
            **options: Additional options passed to nodriver.start():
                - browser_args: List of Chrome arguments
                - browser_executable_path: Path to Chrome binary
                - lang: Browser language (e.g., "en-US")

        Raises:
            ValueError: If ``call_timeout`` or ``navigate_timeout`` is not
                None and not a positive number no larger than
                ``threading.TIMEOUT_MAX``.
        """
        for name, value in (("call_timeout", call_timeout), ("navigate_timeout", navigate_timeout)):
            # concurrent.futures.wait() raises OverflowError above TIMEOUT_MAX.
            if value is not None and not (
                isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(value)
                and 0 < value <= threading.TIMEOUT_MAX
            ):
                raise ValueError(
                    f"{name} must be None or a positive number of seconds"
                    f" no larger than {threading.TIMEOUT_MAX:g}: {value!r}"
                )
        self._headless = headless
        self._profile_dir = profile_dir
        self._default_timeout = default_timeout
        self._call_timeout = call_timeout
        self._navigate_timeout = navigate_timeout
        self._options = options
        self._browser: nodriver.Browser | None = None
        self._page: nodriver.Tab | None = None
        self._started = False
        # The loop the browser lives on, and the private thread running it
        # when the backend created that loop itself (None for a caller's loop).
        self._loop: asyncio.AbstractEventLoop | None = None
        self._private_loop: _PrivateLoop | None = None
        # Serializes start(), start_async(), stop(), and stop_async(), per
        # backend. Not reentrant: start_async() and stop_async() hold it
        # across awaits, so with an RLock a sync call from another coroutine
        # on the same thread would walk into a start or stop in progress.
        # _holding tells that caller apart instead.
        self._lock = threading.Lock()
        self._clear_lock_state()

    def _clear_lock_state(self) -> None:
        """Record the lock as free, with no hold given up on."""
        # The current hold (None while the lock is free), and a count of
        # holds, so a waiter can tell one long hold from a series of short
        # ones and a holder can tell whether its own hold was given up on.
        # One attribute, replaced whole, so a reader never sees half of one.
        self._holding: _Hold | None = None
        self._hold_count = 0
        # The hold waiters gave up on: later waiters give up on it at once.
        self._abandoned: _Abandonment | None = None
        # Makes giving up on a hold one decision, so two waiters giving up
        # at the same moment do not both end Chrome.
        self._give_up_lock = threading.Lock()

    def __getstate__(self) -> dict[str, Any]:
        """Pickle the configuration only, as a stopped backend.

        The event loop and its thread cannot be pickled, and the browser and
        tab belong to that loop, so a copy that kept them would run nodriver
        objects on a loop they were not created on.
        """
        state = self.__dict__.copy()
        state["_loop"] = None
        state["_private_loop"] = None
        state["_started"] = False
        state["_browser"] = None
        state["_page"] = None
        for name in ("_lock", "_holding", "_hold_count", "_abandoned", "_give_up_lock"):
            state.pop(name, None)
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        """Restore the configuration, with a lock of the copy's own.

        A backend pickled by an earlier graftpunk has no loop attributes and
        no call timeouts, so they default to "no loop" and the default
        timeouts here.
        """
        self._loop = None
        self._private_loop = None
        self._call_timeout = DEFAULT_CALL_TIMEOUT_S
        self._navigate_timeout = DEFAULT_NAVIGATE_TIMEOUT_S
        self.__dict__.update(state)
        self._lock = threading.Lock()
        self._clear_lock_state()

    def _ensure_private_loop(self) -> None:
        """Create the private loop ``start()`` runs the browser on, if there is no loop.

        A loop left from an earlier start that is no longer running is
        released first. The caller (``start()``) holds the lock, so two
        threads cannot each create one.

        Raises:
            RuntimeError: If the loop's thread does not start.
        """
        if self._loop is not None and not self._loop.is_running():
            self._release_loop()
        if self._loop is None:
            self._private_loop = _PrivateLoop()
            self._loop = self._private_loop.loop

    def _took_lock(self, method: str, loop: asyncio.AbstractEventLoop | None = None) -> None:
        """Record this thread's *method* (on *loop*, if async) as the holder of the lock it took."""
        self._hold_count += 1
        self._holding = _Hold(self._hold_count, threading.current_thread(), method, loop)

    def _release_lock(self) -> None:
        """Release the lock this thread holds, unless a stop is ending Chrome for this hold.

        That stop gave up on this hold and runs without the lock, so the lock
        stays held until it is done, and ``_end_given_up()`` releases it: a
        start that took the lock meanwhile would have its Chrome forgotten
        and its loop closed by that stop.
        """
        with self._give_up_lock:
            abandoned = self._abandoned
            holding = self._holding
            if (
                abandoned is not None
                and holding is not None
                and abandoned.hold == holding.number
                and abandoned.ender_claimed
                and not abandoned.ended.is_set()
            ):
                abandoned.release_on_end = True
                return
            self._holding = None
        self._lock.release()

    def _end_given_up(self, abandonment: _Abandonment) -> None:
        """Mark a given-up hold's end of Chrome done, and release the lock if its holder returned.

        That release is made from the stop's thread, not the holder's,
        which ``threading.Lock`` allows.
        """
        with self._give_up_lock:
            abandonment.ended.set()
            if not abandonment.release_on_end:
                return
            self._holding = None
        self._lock.release()

    def _acquire_lock(self, method: str) -> "tuple[_Abandonment, bool] | None":
        """Wait for the lock and take it for *method*, blocking this thread.

        Returns:
            None once the lock is held, or the hold the wait gave up on and
            whether this wait was the first to (``_LockWait``), in which
            case the lock is not held.
        """
        wait = _LockWait(self, ends=method.startswith("stop"))
        while not self._lock.acquire(timeout=_EXIT_POLL_S):
            given_up = wait.gave_up_on()
            if given_up is not None:
                return given_up
        self._took_lock(method)
        return None

    async def _acquire_lock_async(self, method: str) -> "tuple[_Abandonment, bool] | None":
        """``_acquire_lock()`` without blocking the running event loop.

        It polls rather than block a worker thread on the lock: a cancelled
        caller then holds nothing, where a worker would take the lock after
        the caller had gone and nothing would release it. The poll also
        keeps the caller's loop running, which a start on another thread may
        be waiting on, and which a holder on this same loop needs in order
        to finish.
        """
        wait = _LockWait(self, ends=method.startswith("stop"))
        while not self._lock.acquire(blocking=False):
            given_up = wait.gave_up_on()
            if given_up is not None:
                return given_up
            await asyncio.sleep(_EXIT_POLL_S)
        self._took_lock(method, asyncio.get_running_loop())
        return None

    def _give_up_on(
        self, holding: _Hold, reason: str, *, ends: bool
    ) -> "tuple[_Abandonment, bool] | None":
        """Give up on *holding*, once for all waiters; None if it ended meanwhile.

        The second value is whether this waiter ends Chrome: true for the
        first stop (*ends*) to give up on the hold, whether or not a start
        gave up on it before. Checked under the lock ``_release_lock()``
        takes, so a stop never claims the end of a hold already released.
        """
        with self._give_up_lock:
            if self._holding is not holding:
                return None
            abandoned = self._abandoned
            if abandoned is None or abandoned.hold != holding.number:
                abandoned = _Abandonment(holding.number, holding.method, reason)
                self._abandoned = abandoned
            first = ends and not abandoned.ender_claimed
            if first:
                abandoned.ender_claimed = True
            return abandoned, first

    def _hold_given_up(self, hold: int | None) -> bool:
        """Whether a waiter gave up on hold number *hold*."""
        abandoned = self._abandoned
        return hold is not None and abandoned is not None and abandoned.hold == hold

    def _held_by_this_thread(self) -> bool:
        """Whether this thread holds the lock.

        True inside a sync ``start()`` or ``stop()`` on this thread, and while
        a ``start_async()`` or ``stop_async()`` on this thread holds it:
        paused at an await, or suspended on a loop that has stopped. The entry
        points use it to refuse a start, or to stop without the lock, rather
        than wait for a lock this thread would never release.
        """
        holding = self._holding
        if holding is None or holding.owner is not threading.current_thread():
            return False
        # A holder that returned while a stop was ending Chrome for its hold
        # left the lock to that stop, and this thread waits for it like any.
        abandoned = self._abandoned
        return not (
            abandoned is not None and abandoned.hold == holding.number and abandoned.release_on_end
        )

    def _held_on_this_loop(self) -> bool:
        """Whether an async call on this thread's running loop holds the lock.

        That holder is a task on this loop, so it can finish while a
        coroutine here polls for the lock, which therefore cannot deadlock.
        """
        holding = self._holding
        return holding is not None and holding.loop is not None and holding.loop is _running_loop()

    def _holding_method(self) -> str | None:
        """The method holding the lock, or None while it is free."""
        holding = self._holding
        return holding.method if holding is not None else None

    def _held_on_this_thread_error(self, method: str) -> RuntimeError:
        """The error a start raises while this thread's own call holds the lock."""
        holder = self._holding_method() or "stop_async"
        doing = "starting" if holder.startswith("start") else "stopping"
        return RuntimeError(
            _HELD_ON_THIS_THREAD_MESSAGE.format(method=method, holder=holder, doing=doing)
        )

    def _release_loop(self) -> None:
        """Forget the browser's loop, stopping it only if this backend created it.

        A caller's loop (the ``BrowserSession.start_async()`` path) is never
        stopped. Never raises.
        """
        private = self._private_loop
        self._loop = None
        self._private_loop = None
        if private is not None:
            private.close()

    def _run_async(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        timeout: float | None = None,
        method: str = "call",
    ) -> Any:
        """Run a coroutine on the browser's event loop and wait for its result.

        With ``timeout`` None the wait has no limit, which is what ``start()``
        and ``stop()`` use: a start sweeps orphaned Chromes and retries its
        connection, and a stop bounds its own waits. The other synchronous
        methods pass ``call_timeout`` or ``navigate_timeout``. When the
        timeout passes, the coroutine is cancelled on the loop and the call
        raises ``TimeoutError``; the loop keeps running, so the next call
        works. A coroutine that finishes before the cancel reaches it has not
        timed out: the call returns its result, or raises its exception.
        Interrupting the wait (Ctrl-C) cancels the coroutine the same
        way and re-raises. Either way the call waits for the cancellation to
        land: up to ``_LOOP_JOIN_TIMEOUT_S`` for the loop to begin the call,
        then up to ``_LOOP_JOIN_TIMEOUT_S`` more for it to finish, so up to
        twice that in all.

        Args:
            coro: Coroutine to execute.
            timeout: Seconds to wait for the result, or None for no limit.
            method: The public method making the call, named in the
                ``TimeoutError`` message.

        Returns:
            Result of the coroutine.

        Raises:
            RuntimeError: If there is no loop (the browser was never started on
                one), if called from the browser's own event loop, which would
                deadlock waiting on itself, if that loop has stopped, or if the
                call was cancelled because the loop is shutting down.
            TimeoutError: If the coroutine did not finish within ``timeout``.
        """
        loop = self._loop
        running = _running_loop()
        if loop is None:
            problem = "NoDriverBackend has no event loop: start() the browser first."
        elif running is loop:
            problem = _IN_OWN_LOOP_MESSAGE
        elif not loop.is_running():
            problem = (
                "The event loop this NoDriverBackend's browser was started on is no longer running."
            )
        else:
            problem = ""
        if problem or loop is None:
            # Close the coroutine to avoid a "coroutine was never awaited" warning.
            coro.close()
            raise RuntimeError(problem)

        started = threading.Event()
        finished = threading.Event()
        # How the coroutine itself ended, for a call that finishes after the
        # wait gave up on it: future.cancel() marks the future cancelled at
        # once, even when the coroutine then runs to the end before the
        # cancel reaches its task, so the future no longer carries the result.
        outcome: dict[str, Any] = {}

        async def tracked() -> Any:
            started.set()
            try:
                outcome["result"] = await coro
                return outcome["result"]
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                outcome["error"] = exc
                raise
            finally:
                finished.set()

        wrapper = tracked()
        try:
            future = asyncio.run_coroutine_threadsafe(wrapper, loop)
        except RuntimeError:
            # The loop closed after the check above.
            wrapper.close()
            coro.close()
            raise

        def cancel_and_wait() -> None:
            # A no-op when the coroutine already finished.
            future.cancel()
            # On a running loop the submitted call takes its first step before
            # the cancel reaches it, so it starts and then unwinds.
            if not started.is_set() and loop.is_running():
                started.wait(_LOOP_JOIN_TIMEOUT_S)
            if started.is_set():
                finished.wait(_LOOP_JOIN_TIMEOUT_S)
            elif not loop.is_running():
                # The loop stopped before it ran the submission and never
                # will: close both coroutines, or each warns "was never
                # awaited" when collected.
                wrapper.close()
                coro.close()

        try:
            # wait() rather than result(timeout): a TimeoutError the coroutine
            # raises itself must not read as the call running out of time.
            if timeout is None or concurrent.futures.wait((future,), timeout).done:
                return future.result()
        except concurrent.futures.CancelledError as exc:
            # The loop cancelled the call: another thread stopped the backend
            # and its teardown cancelled what was still running. RuntimeError
            # is what every caller already treats as "the call could not run".
            raise RuntimeError("NoDriverBackend call was cancelled (browser stopping)") from exc
        except BaseException:
            # A KeyboardInterrupt while waiting must not leave the coroutine
            # running on the loop, and its cleanup (a start releasing a
            # half-started browser) should finish before the caller moves on.
            cancel_and_wait()
            raise
        # The call ran out of time. Cancel it so it does not hold the tab,
        # and leave the loop running for the next call.
        cancel_and_wait()
        # The coroutine ran to the end before the cancel reached it (including
        # between wait() returning and the cancel): it gets its own outcome.
        if "result" in outcome:
            return outcome["result"]
        if "error" in outcome:
            raise outcome["error"]
        raise TimeoutError(f"NoDriverBackend.{method} did not finish within {timeout:g} s")

    async def _start_async(self, _max_attempts: int = 3) -> None:
        """Async implementation of browser start.

        nodriver gives Chrome ~2.75s to accept CDP connections. On macOS this
        sometimes isn't enough (Gatekeeper checks, system load, etc.), so we
        retry the entire ``uc.start()`` call up to *_max_attempts* times with
        a short back-off between attempts.

        A no-op when the backend is already started on a loop that is still
        running, or on this one: launching again would orphan the running
        Chrome, and releasing its loop would strand it. A browser whose loop
        has finished (an earlier ``asyncio.run(session.start_async())`` that
        never stopped) cannot be used any more, so it is ended without its
        loop, on a worker thread, and a new one is started here.

        The caller holds the backend's lock: ``start_async()``, or ``start()``
        on its own thread while this runs on the private loop.
        """
        if self._started and self._browser is not None:
            current = asyncio.get_running_loop()
            old = self._loop
            if old is current or (old is not None and old.is_running()):
                LOG.debug("nodriver_backend_already_started")
                return
            LOG.info("nodriver_backend_replacing_browser_of_finished_loop")
            # Forgotten before the await, so a cancellation during the end
            # leaves the backend stopped rather than started with no browser.
            browser = self._detach_browser()
            self._reset_state()
            await _finish_on_worker_thread(lambda: _end_browser_without_loop(browser))
        try:
            import nodriver as uc
        except ImportError as exc:
            raise BrowserError(
                f"nodriver package not installed. Install with: {install_command('browser')}"
            ) from exc

        _patch_nodriver_cookie_parsing()

        # The browser's objects belong to this loop; every later call runs here.
        loop = asyncio.get_running_loop()
        if loop is not self._loop:
            # A private loop left from an earlier start, now superseded by the
            # caller's loop (BrowserSession.start_async()). Joining its
            # thread blocks, so not on the caller's loop.
            if self._private_loop is not None:
                await asyncio.to_thread(self._release_loop)
            else:
                self._release_loop()
            self._loop = loop
        # This start's hold of the lock when start_async() holds it, so the
        # launch can tell that a waiter gave up on it; a sync start() holds
        # it on another thread and is never given up on.
        holding = self._holding
        hold = holding.number if holding is not None and holding.loop is loop else None
        try:
            await self._launch(uc, _max_attempts, hold)
        except BaseException:
            # A failed start_async() must not leave the caller's loop recorded:
            # a later synchronous start() would find it stopped. The private
            # loop is start()'s to release.
            private = self._private_loop
            if self._loop is loop and (private is None or private.loop is not loop):
                self._loop = None
            raise

    async def _launch(self, uc: Any, max_attempts: int, hold: int | None = None) -> None:
        """Sweep orphaned Chromes, then start Chrome and open its first tab, with retries.

        *hold* is the ``start_async()`` hold of the lock this runs under. A
        call on another thread can give up on that hold while this is
        suspended on a loop that stopped, and then stops the backend without
        the lock. If this loop runs again afterwards, the launch must not
        record a browser on top of that stop: it ends what it launched and
        raises instead, at its next step.
        """
        # Arm the termination handlers if the CLI asked for them, on this
        # thread: signal dispositions can only be set from the main thread,
        # and arming inside the to_thread call below would silently do
        # nothing (#96). A synchronous start() runs this on the private loop's
        # thread, so start() arms them on its own thread first.
        arm_termination_handlers()

        # End the Chromes earlier runs left behind before adding one more
        # (#96). The sweep shells out to ps and sleeps through a grace
        # period, so it runs on a worker thread rather than blocking this
        # event loop.
        report = await asyncio.to_thread(prepare_browser_launch)
        if report:
            LOG.info(
                "chrome_orphans_cleaned",
                reaped=len(report.reaped),
                profiles_removed=len(report.profiles_removed),
            )

        browser_args = base_browser_args()
        if "browser_args" in self._options:
            browser_args.extend(self._options["browser_args"])
        launch_arg = f"{_LAUNCH_SWITCH}={uuid.uuid4().hex}"
        browser_args.append(launch_arg)

        start_kwargs: dict[str, Any] = {
            "headless": self._headless,
            "sandbox": self._options.get("sandbox", False),
            "browser_args": browser_args,
        }

        if self._profile_dir is not None:
            start_kwargs["user_data_dir"] = str(self._profile_dir)

        # Pass through additional options
        if "browser_executable_path" in self._options:
            start_kwargs["browser_executable_path"] = self._options["browser_executable_path"]
        if "lang" in self._options:
            start_kwargs["lang"] = self._options["lang"]

        # A start that fails or is interrupted inside uc.start() never hands
        # back its browser, so the ones it launched are found in nodriver's
        # registry instead (_release_unreturned_browsers).
        registry = _nodriver_registry()
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            self._raise_if_given_up(hold)
            before = set(registry) if registry is not None else set()
            try:
                self._browser = await uc.start(**start_kwargs)
                self._raise_if_given_up(hold)
                # Registered before the first tab: a signal arriving during
                # navigation must still find this browser (#96).
                register_live_browser(self)
                # Get initial page/tab - navigate to blank page
                self._page = await self._browser.get("about:blank")
                self._raise_if_given_up(hold)
                return
            except Exception as exc:
                last_exc = exc
                # Release whatever this attempt left running before the next
                # one overwrites the handle, or before the raise below (#192).
                # On a worker thread: this loop may be the caller's.
                await _finish_on_worker_thread(
                    self._take_attempt_browsers(registry, before, launch_arg)
                )
                if "Failed to connect to browser" not in str(exc):
                    raise
            except BaseException as exc:
                # Cancelled (Ctrl-C during a synchronous start, or a timeout
                # around start_async()) or exiting: no retry, but the
                # half-started browser still has to go, or it stays
                # registered with _started False, where stop() skips it.
                end = self._take_attempt_browsers(registry, before, launch_arg)
                if isinstance(exc, asyncio.CancelledError):
                    await _finish_on_worker_thread(end)
                else:
                    # KeyboardInterrupt or SystemExit raised inside this
                    # coroutine is stopping the loop with it, so an await
                    # would hand control to a loop that is unwinding. Ending
                    # Chrome here blocks it for the few seconds the end takes.
                    end()
                raise
            if attempt < max_attempts:
                LOG.warning(
                    "nodriver_browser_connect_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                await asyncio.sleep(1.0 * attempt)

        raise BrowserError(browser_connect_failure_message(max_attempts)) from last_exc

    def _raise_if_given_up(self, hold: int | None) -> None:
        """Raise ``RuntimeError`` if a waiter gave up on hold number *hold*."""
        if self._hold_given_up(hold):
            raise RuntimeError(_RESUMED_AFTER_GIVE_UP_MESSAGE)

    async def start_async(self) -> None:
        """Start the browser on the running event loop.

        ``BrowserSession.start_async()`` calls this. The browser then belongs
        to this loop: async code awaits nodriver's objects on it, and the
        synchronous methods submit their work to it from other threads.
        Calling it on a backend already started on a running loop does
        nothing; one whose loop has finished is ended and replaced.

        It holds the backend's lock, like ``start()`` and the stops, so a
        ``start()`` or ``start_async()`` on another thread at the same time
        launches no second Chrome, and a stop on another thread waits for
        this start and then stops the browser it launched. It waits for the
        lock by polling, so this loop keeps running meanwhile. A cancellation
        during that wait propagates without starting anything. It also waits
        when the holder is a ``start_async()`` or ``stop_async()`` on this
        same running loop (two tasks starting one backend under
        ``asyncio.gather``, say), since that holder can finish meanwhile; the
        second start then finds the backend started.

        A holder on another thread is waited for while its event loop runs,
        however long it takes, and a synchronous holder without a limit.
        When the holder's loop is closed, or has not run for
        ``_ABANDONED_HOLD_S`` (60 s) straight, this gives up, logs
        ``nodriver_backend_lock_holder_abandoned`` and raises; it never ends
        Chrome. While a stop that gave up on that holder is still ending
        Chrome, this waits for that end first (up to
        ``_GIVEN_UP_END_WAIT_S``), since the lock stays held until the end
        is done, and then starts if the holder has returned meanwhile. If
        the end has not finished by then, it raises: naming the holder if
        that holder has not returned, and saying the end is outstanding if
        it has. The same applies on the holder's own loop once the holder
        has returned and left the lock to that end.

        If this start is itself suspended on a loop that stopped, a stop on
        another thread can give up on it and stop the backend. Should the
        loop run again, the start ends whatever it launched and raises
        rather than mark the backend started.

        Raises:
            BrowserError: If the browser fails to start.
            RuntimeError: While a ``start_async()`` or ``stop_async()`` on
                this thread holds the backend's lock from an event loop other
                than the running one (one left stopped, say), which cannot
                finish while this waits; when the event loop of another
                thread's ``start_async()`` or ``stop_async()`` holding the
                lock is closed or has not run for ``_ABANDONED_HOLD_S``
                seconds; when a stop's end of Chrome for such a holder has
                not finished after ``_GIVEN_UP_END_WAIT_S``; or when this
                start resumed after a call on another thread gave up on it.
        """
        if self._held_by_this_thread() and not self._held_on_this_loop():
            raise self._held_on_this_thread_error("start_async")
        given_up = await self._acquire_lock_async("start_async")
        if given_up is not None:
            raise RuntimeError(self._gave_up_message("start_async", given_up[0]))
        try:
            await self._start_async()
            self._started = True
        finally:
            self._release_lock()

    def _gave_up_message(self, method: str, abandonment: _Abandonment) -> str:
        """Log, and return, why *method* gave up waiting for the lock's holder."""
        LOG.warning(
            "nodriver_backend_lock_holder_abandoned",
            method=method,
            holder=abandonment.holder,
            reason=abandonment.reason,
        )
        template = _GIVEN_UP_END_MESSAGE if abandonment.release_on_end else _ABANDONED_HOLD_MESSAGE
        return template.format(
            method=method,
            holder=abandonment.holder,
            reason=abandonment.reason,
            wait=_GIVEN_UP_END_WAIT_S,
        )

    def start(
        self,
        headless: bool | None = None,
        profile_dir: Path | None = None,
        **options: Any,
    ) -> None:
        """Start the browser.

        Calling it when already started is a no-op, except in the two places
        listed under Raises, where it raises even then. Two threads starting
        one backend launch one browser: the second waits for the first and
        then finds it started. A start that fails or is interrupted
        (Ctrl-C) ends the Chrome it launched before raising. Before it gives
        up on an abandoned ``start_async()`` or ``stop_async()`` (see Raises),
        it waits for a stop's end of Chrome for that holder in progress, as
        ``start_async()`` does.

        Args:
            headless: Override headless setting.
            profile_dir: Override profile directory.
            **options: Additional backend-specific options.

        Raises:
            BrowserError: If browser fails to start.
            RuntimeError: If called from inside the browser's own event loop,
                where waiting for the lock can deadlock (a stop on another
                thread may hold it while it waits on this loop), while a
                ``start_async()`` or ``stop_async()`` on this thread holds the
                backend's lock, which cannot finish while this call blocks
                the thread, or when the event loop of another thread's
                ``start_async()`` or ``stop_async()`` holding the lock is
                closed or has not run for ``_ABANDONED_HOLD_S`` seconds.
        """
        if self._loop is not None and _running_loop() is self._loop:
            raise RuntimeError(_IN_OWN_LOOP_MESSAGE)
        if self._held_by_this_thread():
            raise self._held_on_this_thread_error("start")
        given_up = self._acquire_lock("start")
        if given_up is not None:
            raise RuntimeError(self._gave_up_message("start", given_up[0]))
        try:
            if self._started:
                LOG.debug("nodriver_backend_already_started")
                return
            self._start_locked(headless, profile_dir, options)
        finally:
            self._release_lock()

    def _start_locked(
        self, headless: bool | None, profile_dir: Path | None, options: dict[str, Any]
    ) -> None:
        """The body of ``start()``, run under the backend's lock."""
        # Allow overrides at start time
        if headless is not None:
            self._headless = headless
        if profile_dir is not None:
            self._profile_dir = profile_dir
        self._options.update(options)

        LOG.info(
            "nodriver_backend_starting",
            headless=self._headless,
        )

        # _start_async arms them too, but here it runs on the private loop's
        # thread, where signal.signal cannot (#96).
        arm_termination_handlers()

        try:
            self._ensure_private_loop()
            self._run_async(self._start_async())
            self._started = True
            LOG.info("nodriver_backend_started")
        except BaseException as exc:
            # _launch already released a browser it failed to start. This
            # covers the gap after the start coroutine returned and before
            # _started was set, where an interrupt leaves a registered
            # browser that stop() would skip. A no-op otherwise.
            self._release_failed_attempt()
            self._release_loop()
            if isinstance(exc, BrowserError) or not isinstance(exc, Exception):
                raise
            LOG.error(
                "nodriver_backend_start_failed",
                error=str(exc),
                headless=self._headless,
                profile_dir=str(self._profile_dir) if self._profile_dir else None,
            )
            raise BrowserError(f"Failed to start NoDriver browser: {exc}") from exc

    def _release_failed_attempt(self) -> None:
        """End the browser without its event loop, and forget it. Never raises.

        Used by the stops that cannot use the browser's loop, after an
        interrupted synchronous start or stop, and where a KeyboardInterrupt
        or SystemExit unwinds a coroutine. A coroutine that can await uses
        ``_release_failed_attempt_off_loop()`` instead, and ``_launch`` ends a
        failed attempt through ``_take_attempt_browsers()``.

        ``uc.start()`` returns a live browser whose first CDP call (opening
        the first tab) can still fail. Retrying then overwrote
        ``self._browser``, so nothing ever stopped the earlier Chrome or
        deleted its temp profile (#192). A failure inside ``uc.start()``
        itself, including "Failed to connect to browser", never sets
        ``self._browser``; ``_launch`` ends that Chrome through nodriver's
        registry instead.

        The browser is ended without its loop (see
        ``_end_browser_without_loop``), since there is no working CDP
        connection to ask it politely, and the profile goes with it: this is
        not a signal handler, so it may wait for Chrome to exit and do the
        unbounded work that ``terminate_nodriver_browser`` deliberately leaves
        out.
        """
        browser = self._detach_browser()
        if browser is not None:
            _end_browser_without_loop(browser)

    async def _release_failed_attempt_off_loop(self) -> None:
        """``_release_failed_attempt()`` from a coroutine, ending Chrome on a worker thread.

        A private loop that is not running this coroutine is detached with the
        browser and closed by that worker once Chrome has ended, since its
        child watcher collects Chrome's exit status on Linux: a stop on this
        thread that runs meanwhile (it skips the lock this one holds) then
        finds no loop to close early, and closing it early leaves Chrome a
        zombie with its pipes open. A private loop running this coroutine
        stays attached: the worker closing it would join the loop's thread
        while that thread's teardown waits for this coroutine, which waits
        for the worker. The worker then only ends Chrome, while the loop
        keeps running and its watcher collects Chrome, and the ``finally`` of
        ``_stop_async_holding_lock()`` schedules that loop's stop afterwards.
        """
        browser = self._detach_browser()
        if browser is None:
            return
        private = self._private_loop
        if private is None or private.loop is asyncio.get_running_loop():
            await _finish_on_worker_thread(lambda: _end_browser_without_loop(browser))
            return
        self._private_loop = None
        self._loop = None

        def end_then_close() -> None:
            try:
                _end_browser_without_loop(browser)
            finally:
                private.close()

        await _finish_on_worker_thread(end_then_close)

    def _detach_browser(self) -> Any:
        """Forget the browser, leaving both registries, and return it (or None)."""
        browser = self._browser
        if browser is None:
            return None
        unregister_live_browser(self)
        # Out of nodriver's atexit registry as well, so its handler does not
        # re-stop a browser this code already ended.
        self._deregister_browser(browser)
        self._browser = None
        self._page = None
        return browser

    def _take_attempt_browsers(
        self, registry: set[Any] | None, before: set[Any], launch_arg: str
    ) -> Callable[[], None]:
        """Detach every browser a failed start attempt launched; return the work that ends them.

        The detaching happens here, on the browser's loop; the returned call
        does the waiting, and can run on a worker thread.
        """
        # The returned browser first: detaching it takes it out of nodriver's
        # registry, so the registry scan below does not find it a second time.
        attempt = self._detach_browser()
        browsers = [] if attempt is None else [attempt]
        browsers.extend(_take_unreturned_browsers(registry, before, launch_arg))
        return lambda: _end_browsers_without_loop(browsers)

    def _deregister_browser(self, browser: Any) -> None:
        """Remove *browser* from nodriver's global instance registry.

        nodriver registers an ``atexit`` handler that iterates all registered
        browser instances to stop them and clean up temp profiles, printing
        to stdout in the process. By removing our browser after we've already
        stopped it, we prevent duplicate stop attempts and noisy output.
        """
        registry = _nodriver_registry()
        if registry is not None:
            registry.discard(browser)

    async def _stop_async(self) -> None:
        """Stop the browser and reap its process.

        Note:
            nodriver's Browser.stop() is synchronous; the reap awaits the
            subprocess, which belongs to the browser's loop, so this runs there.
        """
        # Read once: a stop() on another thread, or on this one while this
        # coroutine is paused, can detach the browser meanwhile.
        browser = self._browser
        if browser is not None:
            # Capture the subprocess BEFORE browser.stop(): nodriver's stop()
            # sets browser._process to None when terminate() and kill() both
            # fail (nodriver 0.48.1 and 0.50.6), and reading it after would
            # then silently no-op the reap.
            proc = getattr(browser, "_process", None)
            # try/finally guarantees the reap runs even if browser.stop() or
            # _deregister_browser() raise an unexpected exception (anything
            # outside the (RuntimeError, OSError) handled set). Reaping is
            # most critical exactly on the error paths where Chrome is most
            # likely to leak — don't let a stop() failure also leak the
            # subprocess.
            try:
                try:
                    with _capture_nodriver_stdout():
                        browser.stop()
                except (RuntimeError, OSError) as exc:
                    self._handle_stop_error(exc)
                finally:
                    # Only after nodriver's stop() could not signal Chrome,
                    # the one case where it sets _process to None: handed
                    # back, so a stop that ends this browser without the loop
                    # while the reap below is paused can still find Chrome's
                    # process and SIGKILL it.
                    if proc is not None and getattr(browser, "_process", None) is None:
                        browser._process = proc
                # Remove browser from nodriver's global registry so the atexit
                # handler doesn't re-stop it and print cleanup messages to stdout.
                self._deregister_browser(browser)
            finally:
                # Reap the Chrome subprocess. nodriver's Browser.stop() sends
                # SIGTERM but never awaits proc.wait(), leaving Chrome as a
                # zombie under the live python parent.
                #
                # Edge case: if browser.stop() raised before sending SIGTERM,
                # the helper will pay one _REAP_TERM_TIMEOUT_S wait before
                # the SIGKILL escalation reaps the process. Acceptable:
                # error paths are rare and the delay is bounded by the
                # configured timeout.
                #
                # The removal is nested behind the reap in its own finally,
                # because a reap that raises must not cost the directory.
                try:
                    await _reap_browser_process(proc)
                    # While the loop still runs, so a later garbage
                    # collection does not close it on a closed loop. Reaped,
                    # it closes at once; a wedged Chrome's close is scheduled.
                    _close_transport(proc)
                finally:
                    # nodriver deletes its temp profile only from the atexit
                    # handler that iterates the registry _deregister_browser
                    # just removed us from, so without this every stop leaks
                    # one directory under the temp dir (#96). A custom
                    # profile_dir is never touched.
                    remove_browser_temp_profile(browser)

    def stop(self) -> None:
        """Stop the browser and release resources.

        Idempotent - calling when already stopped is a no-op. When the
        browser's loop has stopped, or this is called from inside that loop
        (which logs ``nodriver_backend_stop_in_own_loop`` at warning level),
        Chrome is ended without the loop: SIGTERM (the subprocess handle on
        Windows), a wait of up to ``_REAP_TERM_TIMEOUT_S`` for it to exit,
        SIGKILL if it has not, and the temp profile's removal once it has.
        Inside the loop that blocks the loop for as long as that takes. An
        interrupt (Ctrl-C) while the stop waits on the loop ends Chrome the
        same way before it propagates.

        A stop called while another thread's ``start()`` is in progress waits
        for that start and then stops the browser it launched, since both
        hold the backend's lock. From inside the browser's own loop it does
        not wait: a start may be holding the lock while it waits on that loop.
        Called while a ``stop_async()`` on this thread holds the lock (paused
        at an await, from another coroutine on its loop, or left suspended on
        a loop that stopped) it does not wait either, since that stop cannot
        resume while this call blocks the thread: it stops the browser itself,
        as above, and that ``stop_async()`` finds the backend stopped if it
        resumes. If that ``stop_async()`` is already ending Chrome without the
        loop (it was cancelled, or the browser's loop had stopped), this call
        finds nothing left to stop and returns before Chrome has exited (up
        to about 4 s before), while that ``stop_async()`` finishes ending
        Chrome. Called while a ``start_async()`` on this thread holds the
        lock, it finds the backend not started yet and returns, and that
        start then finishes and leaves the backend started; ``await
        stop_async()`` on the start's loop waits for the start instead.

        A ``start_async()`` or ``stop_async()`` on another thread that holds
        the lock is waited for while its event loop runs. Once that loop is
        closed, or has not run for ``_ABANDONED_HOLD_S`` seconds straight
        (its task left on an event loop that stopped, say), it is given up
        on: this call logs ``nodriver_backend_lock_holder_abandoned`` and ends
        Chrome without the browser's loop, as above, without the lock, even
        when a start gave up on that holder first (a start that gives up ends
        nothing). A stop that gives up on a hold another stop is already
        ending Chrome for waits for that end (up to ``_GIVEN_UP_END_WAIT_S``)
        and returns. The lock stays held until the end is done: a holder
        that returns meanwhile leaves it to the end, which releases it, so a
        start that comes in during the end waits for it rather than launch a
        browser the end would forget.
        """
        running = _running_loop()
        if self._held_by_this_thread():
            LOG.info(
                "nodriver_backend_stop_while_this_thread_holds_lock", holder=self._holding_method()
            )
            self._stop_holding_lock(running)
            return
        if running is not None and running is self._loop:
            self._stop_holding_lock(running)
            return
        given_up = self._acquire_lock("stop")
        if given_up is not None:
            self._stop_after_giving_up(running, *given_up)
            return
        try:
            self._stop_holding_lock(running)
        finally:
            self._release_lock()

    def _stop_after_giving_up(
        self, running: asyncio.AbstractEventLoop | None, abandonment: _Abandonment, first: bool
    ) -> None:
        """``stop()`` once its wait gave up on the lock's holder, without the lock.

        The first stop to give up on a hold ends Chrome; a later one waits
        for that end, so it does not return while Chrome is still running.
        """
        self._gave_up_message("stop", abandonment)
        if not first:
            if not abandonment.ended.wait(_GIVEN_UP_END_WAIT_S):
                LOG.warning("nodriver_backend_given_up_end_still_running")
            return
        self._end_for_given_up_hold(running, abandonment)

    def _end_for_given_up_hold(
        self, running: asyncio.AbstractEventLoop | None, abandonment: _Abandonment
    ) -> None:
        """End Chrome for a hold this stop gave up on; release the lock if its holder returned."""
        try:
            self._stop_holding_lock(running, without_loop=True)
        finally:
            self._end_given_up(abandonment)

    def _stop_holding_lock(
        self, running: asyncio.AbstractEventLoop | None, *, without_loop: bool = False
    ) -> None:
        """The body of ``stop()``. *running* is the calling thread's running loop, if any.

        With *without_loop*, Chrome is ended without the browser's loop, and
        a browser recorded by a start that never marked the backend started
        is ended too: that start has been given up on.
        """
        if not self._started and not (without_loop and self._browser is not None):
            return

        LOG.info("nodriver_backend_stopping")
        loop = self._loop
        try:
            with suppress_asyncio_noise():
                try:
                    if without_loop or loop is None or loop.is_closed() or not loop.is_running():
                        # Nothing left to run _stop_async on (a caller's loop
                        # that finished), and the browser must not outlive it.
                        self._release_failed_attempt()
                    elif running is loop:
                        # Waiting on the loop from inside it would deadlock.
                        LOG.warning(
                            "nodriver_backend_stop_in_own_loop",
                            hint="In async code, await stop_async() (or use "
                            "'async with BrowserSession(...)'); Chrome was ended "
                            "without its event loop, so this stop blocked the "
                            "loop while Chrome exited and did not reap the "
                            "subprocess.",
                        )
                        self._release_failed_attempt()
                    else:
                        try:
                            self._run_async(self._stop_async())
                        except BaseException:
                            # Interrupted before or during _stop_async, or the
                            # loop went away: _reset_state() below forgets the
                            # browser, so end it now if the stop did not.
                            self._release_failed_attempt()
                            raise
                finally:
                    # Inside the asyncio log suppression: the private loop's
                    # teardown cancels nodriver's leftover tasks, which can
                    # log noise.
                    self._release_loop()
        except (RuntimeError, OSError) as exc:
            self._handle_stop_error(exc)
        finally:
            self._reset_state()

    async def stop_async(self) -> None:
        """Stop the browser from within an existing async event loop.

        Use this instead of ``stop()`` when already running inside an event
        loop (e.g., from ``BrowserSession.__aexit__``), where ``stop()`` would
        block that loop. The stop runs on the browser's loop: directly when
        that is the current loop, otherwise submitted to it and awaited. When
        the browser's loop has stopped, Chrome is ended without it (SIGTERM,
        then SIGKILL if needed, or the subprocess handle on Windows) on a
        worker thread. A cancellation (``asyncio.wait_for``, Ctrl-C under
        ``asyncio.run``) ends Chrome the same way, also on a worker thread,
        before it propagates; a second cancellation meanwhile does not cut
        that end short. Joining the private loop's thread happens on a worker
        thread too, so the caller's loop keeps running meanwhile.

        Like ``stop()``, it waits for a ``start()`` in progress on another
        thread and then stops the browser that start launched. It waits by
        polling the backend's lock, so the caller's loop keeps running (that
        start may be waiting on it), and holds the lock until the stop is
        done. A cancellation during that wait propagates without stopping
        anything, since the stop has not begun. It waits the same way for a
        ``start_async()`` in progress on this same running loop, which can
        finish meanwhile, and then stops the browser that start launched.
        Otherwise it never waits for a lock this thread holds: while another
        ``stop_async()`` on this thread is
        paused at an await (two on one loop, or one left suspended on a loop
        that stopped), it stops the browser itself, as above, and the paused
        one finds the backend stopped if it resumes. If the paused one is
        already ending Chrome without the loop (it was cancelled, or the
        browser's loop had stopped), this call finds nothing left to stop and
        returns before Chrome has exited (up to about 4 s before), while the
        paused one finishes ending Chrome. Either way the private loop is
        closed only after Chrome has exited or the stop has given up on it,
        so the loop's child watcher can still collect Chrome's exit status.

        A ``start_async()`` or ``stop_async()`` on another thread that holds
        the lock is waited for while its event loop runs, and given up on
        once that loop is closed or has not run for ``_ABANDONED_HOLD_S``
        seconds straight, as in ``stop()``: this call logs
        ``nodriver_backend_lock_holder_abandoned`` and ends Chrome without the
        browser's loop, on a worker thread, without the lock, or waits for
        the end another stop that gave up on the same hold is running (up to
        ``_GIVEN_UP_END_WAIT_S``). The lock stays held until that end is
        done, as in ``stop()``. The whole end, including the lock's release,
        runs on that worker thread, so it finishes even if this call's own
        event loop stops meanwhile, and should that loop run again later,
        this call only returns. The exception is a ``stop_async()`` awaited on the
        browser's private loop, whose end runs on that loop, since a worker
        closing the loop would join its thread while that thread waits for
        the worker; that loop runs on its own thread until the end stops it.
        Once the holder has returned and left the lock to such an end, a call
        on the holder's own loop waits for the end up to
        ``_GIVEN_UP_END_WAIT_S``, like a call on any other loop.

        When the browser's loop cancels the stop submitted to it (the loop is
        shutting down) and nobody cancelled this call, Chrome is ended without
        the loop and this returns normally rather than raise a
        ``CancelledError`` its caller never asked for.
        """
        if self._held_by_this_thread() and not (
            self._held_on_this_loop() and self._holding_method() == "start_async"
        ):
            LOG.info(
                "nodriver_backend_stop_while_this_thread_holds_lock", holder=self._holding_method()
            )
            await self._stop_async_holding_lock()
            return
        given_up = await self._acquire_lock_async("stop_async")
        if given_up is not None:
            await self._stop_async_after_giving_up(*given_up)
            return
        try:
            await self._stop_async_holding_lock()
        finally:
            self._release_lock()

    async def _stop_async_after_giving_up(self, abandonment: _Abandonment, first: bool) -> None:
        """``_stop_after_giving_up()`` for ``stop_async()``, polling rather than blocking."""
        self._gave_up_message("stop_async", abandonment)
        if not first:
            deadline = time.monotonic() + _GIVEN_UP_END_WAIT_S
            while not abandonment.ended.is_set():
                if time.monotonic() >= deadline:
                    LOG.warning("nodriver_backend_given_up_end_still_running")
                    return
                await asyncio.sleep(_EXIT_POLL_S)
            return
        private = self._private_loop
        if private is None or private.loop is not asyncio.get_running_loop():
            # The whole end, the lock's release included, runs on a worker
            # thread, as stop() runs it on its own: should this coroutine's
            # loop stop meanwhile, Chrome is still ended and the lock still
            # released, and the coroutine only returns if it resumes.
            await _finish_on_worker_thread(lambda: self._end_for_given_up_hold(None, abandonment))
            return
        # On the browser's private loop, a worker running the end would join
        # this loop's thread while that thread waits for the worker. The end
        # runs here instead, as in a stop_async() holding the lock on this
        # loop, which keeps running until the end has scheduled its stop.
        try:
            await self._stop_async_holding_lock(without_loop=True)
        finally:
            self._end_given_up(abandonment)

    async def _stop_async_holding_lock(self, *, without_loop: bool = False) -> None:
        """The body of ``stop_async()``. *without_loop* as for ``_stop_holding_lock()``."""
        if not self._started and not (without_loop and self._browser is not None):
            return

        LOG.info("nodriver_backend_stopping")
        loop = self._loop
        current = asyncio.get_running_loop()
        try:
            with suppress_asyncio_noise():
                try:
                    if without_loop:
                        await self._release_failed_attempt_off_loop()
                    elif loop is None or loop is current:
                        await self._stop_async()
                    elif loop.is_closed() or not loop.is_running():
                        # Nothing left to run _stop_async on, and the browser
                        # must not outlive the loop it was started on. The
                        # release waits for Chrome to exit, so not on the
                        # caller's loop.
                        await self._release_failed_attempt_off_loop()
                    else:
                        await asyncio.wrap_future(
                            asyncio.run_coroutine_threadsafe(self._stop_async(), loop)
                        )
                except BaseException as exc:
                    # Cancelled before or during _stop_async: _reset_state()
                    # below forgets the browser, so end it now if the stop
                    # did not. Off the caller's loop, as above; a
                    # KeyboardInterrupt or SystemExit raised in this frame is
                    # stopping the loop, so that one ends Chrome here.
                    if not isinstance(exc, asyncio.CancelledError):
                        self._release_failed_attempt()
                        raise
                    await self._release_failed_attempt_off_loop()
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() == 0:
                        # Not this caller's cancellation: the browser's loop
                        # cancelled the stop it was running.
                        LOG.debug("nodriver_backend_stop_cancelled_by_its_loop")
                        return
                    raise
                finally:
                    # Chrome is stopped or being ended by now. Forgotten
                    # before the loop is released, so a stop on this thread
                    # that runs during the release (it skips the lock) does
                    # not stop the browser again, on a loop it does not
                    # belong to.
                    self._detach_browser()
                    if loop is None or loop is current:
                        # Nothing to join from here: a private loop running
                        # this coroutine only schedules its own stop.
                        self._release_loop()
                    else:
                        # Joining the private loop's thread blocks, so not on
                        # the caller's loop.
                        await asyncio.to_thread(self._release_loop)
        except (RuntimeError, OSError) as exc:
            self._handle_stop_error(exc)
        finally:
            self._reset_state()

    def _terminate_for_signal(self) -> None:
        """End the browser process from a signal handler, then reset this backend.

        The loop-free path (:class:`graftpunk.signals.TerminatableBrowser`).
        ``stop()`` waits on the browser's event loop and ``stop_async()`` needs
        a running one; a signal handler can do neither. The private loop's
        thread is left running: it is a daemon, so it cannot keep the dying
        process alive, and a backend that survives the signal starts its next
        browser on it. The sequence itself lives in
        ``terminate_nodriver_browser``, which the ``gp observe`` handle shares:
        one SIGTERM and one log line, no directory removal, because a handler
        must not do unbounded work on its way out. The temp profile is left for
        a later launch's stale sweep.

        It finishes with ``_reset_state()`` so the object is coherent
        afterwards: ``is_running`` must not claim a browser this method just
        ended, and the registry entry goes with it.
        """
        browser = self._browser
        if browser is None:
            return
        terminate_nodriver_browser(browser)
        self._reset_state()

    @property
    def is_running(self) -> bool:
        """Whether the browser is currently started.

        Returns:
            True if browser is started, False otherwise.
        """
        return self._started and self._browser is not None

    async def _navigate_async(self, url: str) -> None:
        """Async implementation of navigation."""
        assert self._browser is not None  # Type narrowing for ty
        self._page = await self._browser.get(url)

    async def _stop_loading_async(self) -> None:
        """Send ``Page.stopLoading`` to the tab ``navigate()`` navigates."""
        import nodriver.cdp.page as cdp_page

        # Browser.get navigates the browser's first page target, which is not
        # self._page once the recorded tab has been closed, so the stop goes
        # to the same target. Browser.tabs lists the page targets in that
        # order in nodriver 0.48 and 0.50 (main_tab picks differently in 0.48).
        browser = self._browser
        tabs = browser.tabs if browser is not None else []
        tab = tabs[0] if tabs else self._page
        if tab is None:
            return
        await tab.send(cdp_page.stop_loading())

    def _stop_pending_navigation(self) -> None:
        """Stop the load a timed-out ``navigate()`` left running in Chrome.

        Best effort: a Chrome that does not answer within
        ``_STOP_LOADING_TIMEOUT_S`` (a frozen one) gets the command when it
        resumes, and any failure is logged and swallowed, so the caller still
        sees the navigate's own ``BrowserError``.
        """
        try:
            self._run_async(
                self._stop_loading_async(),
                timeout=_STOP_LOADING_TIMEOUT_S,
                method="navigate() stopping the load",
            )
        except Exception as exc:  # noqa: BLE001 - best effort; the navigate's error is what the caller gets
            LOG.warning(
                "nodriver_backend_stop_loading_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )

    def navigate(self, url: str) -> None:
        """Navigate to a URL.

        Args:
            url: URL to navigate to.

        Raises:
            BrowserError: If navigation fails, the browser cannot start, or
                the navigation does not finish within ``navigate_timeout``.
                On a timeout, Chrome is told to stop loading the URL first
                (a wait with its own 2 s limit), so the tab stays on the page it
                showed before and the next call works.
        """
        if not self.is_running:
            self.start()

        LOG.debug("nodriver_backend_navigating", url=url)
        try:
            self._run_async(
                self._navigate_async(url), timeout=self._navigate_timeout, method="navigate()"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                self._stop_pending_navigation()
            LOG.error("nodriver_backend_navigation_failed", url=url, error=str(exc))
            raise BrowserError(f"Navigation failed: {exc}") from exc

    async def _get_current_url_async(self) -> str:
        """Async implementation of current URL retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("window.location.href")
            return str(result) if result else ""
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is the fallback + warning
            # nodriver raises ProtocolException (a plain Exception subclass) for
            # CDP-level errors; a transport-only tuple let it escape (#152).
            LOG.warning(
                "nodriver_get_current_url_failed", error=str(exc), exc_type=type(exc).__name__
            )
            return ""

    @property
    def current_url(self) -> str:
        """Get the current page URL.

        Returns:
            Current URL, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the URL (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(
                self._get_current_url_async(), timeout=self._call_timeout, method="current_url"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; warn user
            LOG.warning("nodriver_current_url_failed", error=str(exc))
            return ""

    async def _get_page_title_async(self) -> str:
        """Async implementation of page title retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("document.title")
            return str(result) if result else ""
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is the fallback + warning
            # nodriver raises ProtocolException (a plain Exception subclass) for
            # CDP-level errors; a transport-only tuple let it escape (#152).
            LOG.warning(
                "nodriver_get_page_title_failed", error=str(exc), exc_type=type(exc).__name__
            )
            return ""

    @property
    def page_title(self) -> str:
        """Get the current page title.

        Returns:
            Page title, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the title (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(
                self._get_page_title_async(), timeout=self._call_timeout, method="page_title"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; warn user
            LOG.warning("nodriver_page_title_failed", error=str(exc))
            return ""

    async def _get_page_source_async(self) -> str:
        """Async implementation of page source retrieval."""
        if self._page is None:
            return ""
        try:
            return await self._page.get_content() or ""
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is the fallback + warning
            # nodriver raises ProtocolException (a plain Exception subclass) for
            # CDP-level errors; a transport-only tuple let it escape (#152).
            LOG.warning(
                "nodriver_get_page_source_failed", error=str(exc), exc_type=type(exc).__name__
            )
            return ""

    @property
    def page_source(self) -> str:
        """Get the current page HTML source.

        Returns:
            Page HTML source, or empty string if:
            - Browser is not running
            - No page is loaded
            - An error occurred retrieving the source (logged at warning level)
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(
                self._get_page_source_async(), timeout=self._call_timeout, method="page_source"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; warn user
            LOG.warning("nodriver_page_source_failed", error=str(exc))
            return ""

    @property
    def driver(self) -> Any:
        """Get the underlying nodriver Browser object.

        Starts the browser if not already running (lazy initialization).

        Returns:
            nodriver.Browser instance.

        Note:
            Using this directly couples code to nodriver.
            Prefer protocol methods when possible.
        """
        if not self.is_running:
            self.start()
        return self._browser

    async def _get_cookies_async(self) -> list[Cookie]:
        """Async implementation of cookie retrieval."""
        if self._page is None:
            return []
        # nodriver.Tab has no get_cookies() (not in 0.48 or 0.50), so read the
        # jar through CDP (issue #193).
        try:
            import nodriver.cdp.storage as cdp_storage

            cdp_cookies = await self._page.send(cdp_storage.get_cookies())
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is the fallback + warning
            # nodriver raises ProtocolException (a plain Exception subclass) for
            # CDP-level errors; a transport-only tuple let it escape (#152).
            LOG.warning("nodriver_get_cookies_failed", error=str(exc), exc_type=type(exc).__name__)
            return []
        return [_cookie_from_cdp(c) for c in cdp_cookies or []]

    def get_cookies(self) -> list[Cookie]:
        """Get all cookies from the browser.

        Reads every cookie in the browser's jar with CDP ``Storage.getCookies``.

        Note:
            Unlike ``set_cookies()`` and ``navigate()``, this method does NOT
            auto-start the browser.

        Returns:
            List of Cookie dicts with ``name``, ``value``, ``domain``, ``path``,
            ``secure``, and ``httponly``, plus ``sameSite`` when the cookie has
            one and ``expires`` (Unix seconds) unless it is a session cookie.
            Empty if the browser is not running, there is no page, or the CDP
            call fails (logged at warning level).
        """
        if not self.is_running:
            return []
        try:
            return self._run_async(
                self._get_cookies_async(), timeout=self._call_timeout, method="get_cookies()"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; warn user
            LOG.warning("nodriver_cookies_failed", error=str(exc))
            return []

    async def _set_cookies_async(self, cookies: list[Cookie]) -> int:
        """Async implementation of cookie setting.

        Returns:
            How many of ``cookies`` are in the jar after setting, read back
            with ``Storage.getCookies``. 0 if there is no page or a CDP call
            fails. Each shortfall is logged at warning level.
        """
        if self._page is None:
            LOG.warning(
                "nodriver_backend_cookie_set_no_page",
                cookie_count=len(cookies),
                is_running=self.is_running,
                browser_present=self._browser is not None,
            )
            return 0

        import nodriver.cdp.storage as cdp_storage

        params = []
        wanted: list[Cookie] = []
        for cookie in cookies:
            param, reason = _cookie_param_from_dict(cookie)
            if param is None:
                LOG.warning(
                    "nodriver_set_cookies_skipped",
                    cookie_name=cookie.get("name", "<unknown>"),
                    reason=reason,
                )
                continue
            params.append(param)
            wanted.append(cookie)
        if not params:
            return 0

        # nodriver.Tab has no set_cookies() (not in 0.48 or 0.50), so write
        # through CDP (issue #193), then read the jar back so the count reports
        # what the browser holds rather than what was requested.
        try:
            await self._page.send(cdp_storage.set_cookies(params))
            jar = await self._page.send(cdp_storage.get_cookies())
        except Exception as exc:  # noqa: BLE001 — best-effort: set_cookies() documents no exception
            LOG.warning(
                "nodriver_set_cookies_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
                cookie_names=[c.get("name", "<unknown>") for c in wanted],
            )
            return 0

        missing = [c.get("name", "<unknown>") for c in wanted if not _jar_has(jar or [], c)]
        if missing:
            LOG.warning(
                "nodriver_set_cookies_incomplete",
                # The cookies sent; a skipped one already logged its own warning.
                requested=len(wanted),
                present=len(wanted) - len(missing),
                missing_names=missing,
            )
        return len(wanted) - len(missing)

    def set_cookies(self, cookies: list[Cookie]) -> int:
        """Set cookies in the browser.

        Writes the cookies with CDP ``Storage.setCookies``, then reads the jar
        back to count them. This is a best-effort operation: failures are
        logged at warning level but no exception is raised. Cookie values are
        never logged.

        Args:
            cookies: List of Cookie dicts to set. Each must have 'name',
                'value', and 'domain' (CDP cannot place a cookie without a
                domain or URL). A cookie missing one of them, or whose
                'expires' is not a finite number, is skipped with a
                ``nodriver_set_cookies_skipped`` warning naming the reason.
                'path' defaults to '/', also when empty. The flags are read
                from 'secure', 'httponly', 'sameSite' ('Strict', 'Lax', or
                'None', any case), and 'expires' (Unix seconds; omit it for a
                session cookie).

        Returns:
            How many of the given cookies are present in the browser after
            setting, matched by name, value, domain, and the path it was set
            at, so an overwrite the browser refused does not count. Returns 0 if
            no page is available or a CDP call fails.
        """
        if not self.is_running:
            self.start()

        try:
            return self._run_async(
                self._set_cookies_async(cookies),
                timeout=self._call_timeout,
                method="set_cookies()",
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; best-effort operation
            LOG.warning(
                "nodriver_backend_cookie_set_failed",
                error=str(exc),
                cookie_count=len(cookies),
                cookie_names=[c.get("name", "<unknown>") for c in cookies],
                is_running=self.is_running,
            )
            return 0

    async def _delete_all_cookies_async(self) -> bool:
        """Async implementation of cookie deletion.

        Returns:
            True if cookies were deleted or there was no page (nothing to
            delete), False if the CDP call failed (logged at warning level).
        """
        if self._page is None:
            LOG.debug("nodriver_delete_cookies_no_page")
            return True  # No page = no cookies to delete
        # nodriver doesn't have delete_all_cookies, so use CDP directly.
        # send() takes the CDP generator, not a method-name string: nodriver
        # calls next() on it, so a string raised TypeError (issue #152).
        try:
            import nodriver.cdp.network as cdp_network

            await self._page.send(cdp_network.clear_browser_cookies())
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is False + warning
            # nodriver raises ProtocolException (a plain Exception subclass)
            # for CDP-level errors, which the old transport-only tuple let
            # escape (issue #152).
            LOG.warning(
                "nodriver_clear_cookies_cdp_failed", error=str(exc), exc_type=type(exc).__name__
            )
            return False

    def delete_all_cookies(self) -> bool:
        """Delete all cookies from the browser.

        Returns:
            True if cookies were successfully deleted or browser was not running
            (no-op), False if an error occurred (error is logged at warning level).
        """
        if not self.is_running:
            return True  # No cookies to delete
        try:
            return self._run_async(
                self._delete_all_cookies_async(),
                timeout=self._call_timeout,
                method="delete_all_cookies()",
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; best-effort operation
            LOG.warning(
                "nodriver_backend_delete_cookies_failed",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return False

    async def _get_user_agent_async(self) -> str:
        """Async implementation of user agent retrieval."""
        if self._page is None:
            return ""
        try:
            result = await self._page.evaluate("navigator.userAgent")
            return str(result) if result else ""
        except Exception as exc:  # noqa: BLE001 — best-effort: the documented contract is the fallback + warning
            # nodriver raises ProtocolException (a plain Exception subclass) for
            # CDP-level errors; a transport-only tuple let it escape (#152).
            LOG.warning(
                "nodriver_get_user_agent_failed", error=str(exc), exc_type=type(exc).__name__
            )
            return ""

    def get_user_agent(self) -> str:
        """Get the browser's User-Agent string.

        Returns:
            User-Agent string from navigator.userAgent,
            or empty string if browser is not running or an error occurs
            (errors logged at warning level).
        """
        if not self.is_running:
            return ""
        try:
            return self._run_async(
                self._get_user_agent_async(), timeout=self._call_timeout, method="get_user_agent()"
            )
        except (RuntimeError, ConnectionError, TimeoutError) as exc:
            # the CDP call can fail if the browser crashed; warn user
            LOG.warning("nodriver_user_agent_failed", error=str(exc))
            return ""

    def get_state(self) -> dict[str, Any]:
        """Get serializable state for session persistence.

        Returns:
            Dict with backend configuration for recreation.
        """
        state: dict[str, Any] = {
            "backend_type": self.BACKEND_TYPE,
            "headless": self._headless,
            "default_timeout": self._default_timeout,
            "call_timeout": self._call_timeout,
            "navigate_timeout": self._navigate_timeout,
        }
        if self._profile_dir is not None:
            state["profile_dir"] = str(self._profile_dir)
        state.update(self._options)
        return state

    @classmethod
    def from_state(cls, state: dict[str, Any]) -> Self:
        """Recreate backend instance from serialized state.

        Args:
            state: Dict from get_state() call.

        Returns:
            New NoDriverBackend instance (not started).
        """
        # Warn if backend_type doesn't match
        saved_backend = state.get("backend_type")
        if saved_backend and saved_backend != cls.BACKEND_TYPE:
            LOG.warning(
                "backend_type_mismatch",
                expected=cls.BACKEND_TYPE,
                saved=saved_backend,
                hint=f"State was saved by {saved_backend} backend but is being "
                f"restored as {cls.BACKEND_TYPE}. Some settings may not apply.",
            )

        # Extract known parameters
        headless = state.get("headless", False)
        default_timeout = state.get("default_timeout", 15)
        call_timeout = state.get("call_timeout", DEFAULT_CALL_TIMEOUT_S)
        navigate_timeout = state.get("navigate_timeout", DEFAULT_NAVIGATE_TIMEOUT_S)

        profile_dir = state.get("profile_dir")
        if profile_dir is not None:
            profile_dir = Path(profile_dir)

        # Remaining items are additional options
        known_keys = (
            "backend_type",
            "headless",
            "default_timeout",
            "call_timeout",
            "navigate_timeout",
            "profile_dir",
        )
        options = {k: v for k, v in state.items() if k not in known_keys}

        return cls(
            headless=headless,
            profile_dir=profile_dir,
            default_timeout=default_timeout,
            call_timeout=call_timeout,
            navigate_timeout=navigate_timeout,
            **options,
        )

    def __enter__(self) -> Self:
        """Context manager entry - start browser.

        Returns:
            Self for use in with statement.
        """
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        """Context manager exit - stop browser."""
        self.stop()

    def __repr__(self) -> str:
        """Return string representation.

        Returns:
            String showing backend type and status.
        """
        status = "running" if self.is_running else "stopped"
        mode = "headless" if self._headless else "headed"
        return f"<NoDriverBackend {mode} {status}>"
