"""The browser behind ``gp observe go`` and ``gp observe interactive``.

A leaf of the CLI package: ``observe_commands`` imports these coroutines, and
nothing here imports a command module back.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from graftpunk import signals
from graftpunk.browser_launch import (
    NodriverBrowserHandle,
    arm_termination_handlers,
    prepare_browser_launch,
)
from graftpunk.chrome_orphans import base_browser_args, remove_browser_temp_profile
from graftpunk.logging import get_logger
from graftpunk.observe import OBSERVE_BASE_DIR
from graftpunk.observe.run import make_run_id, save_observe_run
from graftpunk.observe.storage import session_dirname

console = Console()
LOG = get_logger(__name__)


@dataclass(frozen=True)
class ObserveSession:
    """What a set-up observe run hands back to the command that drives it.

    Attributes:
        browser: The ``nodriver`` browser, already navigated to the URL.
        handle: Its entry in the termination-signal registry.
        tab: The post-navigation tab.
        storage: Where this run's capture is written.
        backend: The capture backend, already started.
    """

    browser: Any
    handle: NodriverBrowserHandle
    tab: Any
    storage: Any
    backend: Any


def stop_observe_browser(browser: Any, handle: NodriverBrowserHandle) -> None:
    """Stop the observe browser, leave the signal registry, and delete its temp profile.

    nodriver removes that directory only from its own atexit handler, which
    never runs for a browser we stopped ourselves, so every observe run used to
    leak one directory under the temp dir (#96). The orderly stop path is where
    that deletion belongs; the signal handler does not do it. Unregistering and
    removing the profile happen in the finally, so a raising stop still leaves
    both done.
    """
    try:
        browser.stop()
    finally:
        signals.unregister_live_browser(handle)
        remove_browser_temp_profile(browser)


async def setup_observe_session(
    namespace: str,
    url: str,
    max_body_size: int,
    headless: bool,
    *,
    session_name: str | None = None,
) -> ObserveSession | None:
    """Set up browser, optionally inject cookies, initialize capture, and navigate to URL.

    When ``session_name`` is provided, loads the cached session and injects
    cookies into the browser. When ``session_name`` is ``None`` (--no-session),
    the browser opens without cookies and capture proceeds normally.

    Args:
        namespace: Storage namespace for ObserveStorage (e.g., site name).
        url: URL to navigate to.
        max_body_size: Max response body size for capture.
        headless: Whether to run browser headless.
        session_name: Session name for cookie injection, or None to skip.

    Returns:
        The set-up :class:`ObserveSession`, or None on failure.
    """

    import nodriver

    from graftpunk import load_session
    from graftpunk.exceptions import SessionExpiredError, SessionNotFoundError
    from graftpunk.observe.capture import NodriverCaptureBackend
    from graftpunk.observe.storage import ObserveStorage
    from graftpunk.session import inject_cookies_to_nodriver

    session = None
    if session_name:
        try:
            session = load_session(session_name)
        except SessionNotFoundError:
            console.print(
                f"[red]Session '{escape(session_name)}' not found.[/red]\n"
                f"[dim]Run 'gp session list' to see available sessions, "
                f"or use --no-session to proceed without cookies.[/dim]"
            )
            return None
        except SessionExpiredError as exc:
            console.print(
                f"[red]Session '{escape(session_name)}' is expired or corrupted.[/red]\n"
                f"[dim]{escape(str(exc))}[/dim]\n"
                f"[dim]Use --no-session to proceed without cookies.[/dim]"
            )
            return None
        except Exception as exc:  # noqa: BLE001 — CLI boundary: user-friendly error
            LOG.error("session_load_failed", session_name=session_name, error=str(exc))
            console.print(
                f"[red]Failed to load session '{escape(session_name)}': {escape(str(exc))}[/red]"
            )
            return None
    else:
        console.print("[dim]No session — opening browser without cookies[/dim]")

    # Arm the termination handlers on this thread: signal dispositions can
    # only be set from the main thread, so arming inside the sweep's worker
    # thread below would silently do nothing (#96).
    arm_termination_handlers()

    # End the Chromes earlier runs left behind before adding one more (#96).
    # gp observe launches nodriver directly rather than through
    # NoDriverBackend, so it calls the same helpers the backend does. This
    # setup helper serves both observe subcommands, go and interactive, so it
    # says what it did either way.
    report = await asyncio.to_thread(prepare_browser_launch)
    if report.reaped:
        console.print(
            f"[dim]Cleaned up {len(report.reaped)} orphaned Chrome process(es) "
            f"from earlier runs.[/dim]"
        )

    # Isolate Chrome from SIGINT: child process inherits SIG_IGN disposition,
    # so Ctrl+C only reaches Python. We save data, then explicitly stop Chrome.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Retry logic matching NoDriverBackend._start_async()
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                browser = await nodriver.start(
                    headless=headless,
                    sandbox=False,
                    browser_args=base_browser_args(),
                )
                break
            except Exception as exc:
                last_exc = exc
                if "Failed to connect to browser" not in str(exc):
                    raise
                if attempt < max_attempts:
                    LOG.warning(
                        "observe_browser_connect_retry",
                        attempt=attempt,
                        max_attempts=max_attempts,
                    )
                    await asyncio.sleep(1.0 * attempt)
        else:
            raise RuntimeError(
                f"Failed to connect to browser after {max_attempts} attempts. "
                "Chrome may be slow to start — try again, or check for stale "
                "Chrome processes."
            ) from last_exc
    finally:
        signal.signal(signal.SIGINT, old_sigint)

    handle = NodriverBrowserHandle(browser)
    signals.register_live_browser(handle)
    try:
        tab = browser.main_tab

        if session is not None:
            injected, filtered = await inject_cookies_to_nodriver(tab, session.cookies)
            msg = f"[dim]Injected {injected} cookie(s)"
            if filtered:
                msg += f" ({filtered} bot-detection cookie(s) filtered)"
            msg += "[/dim]"
            console.print(msg)

        run_id = make_run_id()
        # The run dir is keyed by the slugified session name — the same
        # mapping every observe reader applies (#151).
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_dirname(namespace), run_id)
        bodies_dir = storage.run_dir / "bodies"
        backend = NodriverCaptureBackend(
            browser,
            get_tab=lambda: tab,
            bodies_dir=bodies_dir,
            max_body_size=max_body_size,
        )
        await backend.start_capture_async()

        tab = await browser.get(url)
        return ObserveSession(
            browser=browser, handle=handle, tab=tab, storage=storage, backend=backend
        )
    except Exception:
        stop_observe_browser(browser, handle)
        raise


async def run_observe_go(
    namespace: str, url: str, wait: float, max_body_size: int, *, session_name: str | None = None
) -> None:
    """Async implementation of observe go."""
    observed = await setup_observe_session(
        namespace, url, max_body_size, headless=True, session_name=session_name
    )
    if observed is None:
        raise typer.Exit(1)

    try:
        await observed.tab.sleep(wait)
        await save_observe_run(observed.storage, observed.backend, "observe-go", console=console)
    finally:
        stop_observe_browser(observed.browser, observed.handle)


async def run_observe_interactive(
    namespace: str, url: str, max_body_size: int, *, session_name: str | None = None
) -> None:
    """Async implementation of observe interactive."""

    observed = await setup_observe_session(
        namespace, url, max_body_size, headless=False, session_name=session_name
    )
    if observed is None:
        raise typer.Exit(1)

    try:
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        try:
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
        except NotImplementedError:
            console.print(
                "[red]Interactive mode requires Unix/macOS (signal handlers unavailable).[/red]"
            )
            return

        console.print("\n[bold]Recording... press Ctrl+C to stop and save[/bold]")
        console.print(
            "[dim]Tip: Use Ctrl+C in this terminal for a complete capture "
            "(screenshot, page source, and network data). "
            "Closing the browser window will preserve network data only.[/dim]\n"
        )

        try:
            await stop_event.wait()
        finally:
            loop.remove_signal_handler(signal.SIGINT)

        console.print("\n[dim]Recording stopped. Saving capture...[/dim]")
        await save_observe_run(
            observed.storage, observed.backend, "interactive-final", console=console
        )
    finally:
        stop_observe_browser(observed.browser, observed.handle)
