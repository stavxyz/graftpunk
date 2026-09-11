"""Termination signals end the browsers this process started (#96).

``BrowserSession.__exit__``, ``quit()`` and nodriver's own ``atexit`` handler
all run only when Python exits normally. A SIGTERM from a supervisor, or the
SIGHUP a closing terminal sends, kills Python where it stands and leaves Chrome
running. This module installs the handlers that close that gap.

The registry holds browser HANDLES, not sessions: anything that can end one
browser without an event loop. ``NoDriverBackend`` registers itself once its
browser is up; the two launch sites that drive a ``nodriver`` browser directly,
``gp observe`` and browser token extraction, register the one adapter they
share, ``graftpunk.browser_launch.NodriverBrowserHandle``. Nothing here knows
what a ``BrowserSession`` is, and a future backend gets the behaviour by
implementing one method.

Nothing is installed by importing graftpunk. The CLI sets :data:`auto_install`
in its root callback and the first browser launch arms the handlers, so a
command that never opens a browser leaves the process's signal dispositions
exactly as it found them. A host program can call
:func:`install_termination_cleanup` itself, and a slot it already handles is
left alone either way.

SIGKILL and the OOM killer stay uncatchable, which is why the pre-launch
reaper exists (:mod:`graftpunk.chrome_orphans`).
"""

from __future__ import annotations

import os
import signal
import weakref
from types import FrameType
from typing import Protocol, runtime_checkable

from graftpunk.logging import get_logger

LOG = get_logger(__name__)

# Set True by the CLI's root callback, read at the first browser launch.
auto_install: bool = False

_HANDLED_SIGNAL_NAMES = ("SIGTERM", "SIGHUP")


@runtime_checkable
class TerminatableBrowser(Protocol):
    """Something that can end one browser without touching an event loop."""

    def _terminate_for_signal(self) -> None:
        """End this browser now, from a signal handler.

        The name is private on purpose: this is for the handler in this
        module, not for application code, which stops a browser through
        ``BrowserSession`` or the backend's ``stop()`` and gets an orderly
        shutdown. An implementation must not raise, must not await, and must
        leave its object in a coherent state.
        """
        ...  # pragma: no cover - protocol declaration


_LIVE_BROWSERS: weakref.WeakSet[TerminatableBrowser] = weakref.WeakSet()


def register_live_browser(handle: TerminatableBrowser) -> None:
    """Add *handle* to the set the termination handler will end.

    Held weakly: registering never keeps a browser alive, so a caller that
    forgets to unregister leaks nothing.
    """
    _LIVE_BROWSERS.add(handle)


def unregister_live_browser(handle: TerminatableBrowser) -> None:
    """Remove *handle*. A no-op when it was never registered."""
    _LIVE_BROWSERS.discard(handle)


def live_browsers() -> list[TerminatableBrowser]:
    """A snapshot of the registered handles.

    A snapshot rather than the set itself: the handler unregisters handles as
    it ends them, and it must not iterate a WeakSet that is being mutated,
    whether by itself or by the garbage collector.
    """
    return list(_LIVE_BROWSERS)


def _handle_termination(signum: int, _frame: FrameType | None) -> None:
    """End every live browser, then die of the signal that arrived.

    Restoring ``SIG_DFL`` and re-raising, rather than calling ``sys.exit``,
    keeps the exit status the conventional 128 + signum, so a supervisor
    watching this process still sees the real cause of death. That restore
    and re-raise live in a ``finally``, so they run even if a handle raises
    something other than ``Exception``, or a log call itself raises: nothing
    here may turn a signal into a hang or a swallowed signal.

    Every handle's call is wrapped in ``except BaseException``, not
    ``except Exception``: a handle that raises ``SystemExit`` or
    ``KeyboardInterrupt`` must not stop the others from being asked.
    """
    name = signal.Signals(signum).name
    try:
        LOG.info("termination_signal_received", signal=name)
        for handle in live_browsers():
            try:
                handle._terminate_for_signal()
            except BaseException as exc:  # noqa: BLE001 - a handler has nowhere to raise
                LOG.warning(
                    "termination_cleanup_failed",
                    signal=name,
                    error=str(exc),
                    exc_info=True,
                )
    finally:
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)


def install_termination_cleanup() -> None:
    """Take the SIGTERM and SIGHUP slots, if nobody else has them.

    Idempotent: after the first call the slots no longer hold ``SIG_DFL``, so a
    second call finds them taken and leaves them alone. Signal dispositions can
    only be set from the main thread, and not every platform has SIGHUP, so
    both cases are debug logs rather than failures.
    """
    for name in _HANDLED_SIGNAL_NAMES:
        signum = getattr(signal, name, None)
        if signum is None:
            LOG.debug("termination_handler_not_installed", signal=name, reason="no_such_signal")
            continue
        try:
            current = signal.getsignal(signum)
        except (OSError, ValueError) as exc:
            LOG.debug("termination_handler_not_installed", signal=name, reason=str(exc))
            continue
        if current is not signal.SIG_DFL:
            LOG.debug("termination_handler_not_installed", signal=name, reason="slot_in_use")
            continue
        try:
            signal.signal(signum, _handle_termination)
        except (OSError, ValueError) as exc:
            LOG.debug("termination_handler_not_installed", signal=name, reason=str(exc))
