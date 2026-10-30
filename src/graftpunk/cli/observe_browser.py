"""The browser behind ``gp observe go`` and ``gp observe interactive``.

A leaf of the CLI package: ``observe_commands`` imports these coroutines, and
nothing here imports a command module back.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import signal
import string
import sys
from dataclasses import dataclass
from typing import Any

import typer
from rich.console import Console
from rich.markup import escape

from graftpunk import signals
from graftpunk.browser_launch import (
    CHROME_SHARES_SESSION_CONSEQUENCE,
    NodriverBrowserHandle,
    arm_termination_handlers,
    browser_connect_failure_message,
    check_chrome_in_own_session,
    chrome_in_own_session,
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
        own_session: False when Chrome is known to share this process's
            session, so a terminal's Ctrl+C also stops it (#333).
    """

    browser: Any
    handle: NodriverBrowserHandle
    tab: Any
    storage: Any
    backend: Any
    own_session: bool = True


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
    failure_step: bool = False,
) -> ObserveSession | None:
    """Set up browser, optionally inject cookies, initialize capture, and navigate to URL.

    When ``session_name`` is provided, loads the cached session and injects
    cookies into the browser. When ``session_name`` is ``None`` (--no-session),
    the browser opens without cookies and capture proceeds normally.

    Args:
        namespace: Storage namespace for ObserveStorage (e.g., site name).
        url: URL to navigate to.
        max_body_size: Largest request or response body kept in the HAR; a
            larger one is written to the run's bodies/ directory.
        headless: Whether to run browser headless.
        session_name: Session name for cookie injection, or None to skip.
        failure_step: Record the wrong-password step of ``gp observe interactive
            --wrong-password-first`` (the backend's ``mark_failed_login``).

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
        except Exception as exc:  # broad by design: CLI boundary, user-friendly error
            LOG.error("session_load_failed", session_name=session_name, error=str(exc))
            console.print(
                f"[red]Failed to load session '{escape(session_name)}': {escape(str(exc))}[/red]"
            )
            return None
    else:
        console.print("[dim]No session: opening the browser without cookies[/dim]")

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

    # Chrome starts in a session of its own, so the Ctrl+C that stops the
    # recording reaches only Python, which saves the run and then stops Chrome
    # (#333). SIGINT is also ignored for the length of the start, so a Ctrl+C
    # cannot interrupt the start half way, before the browser is registered.
    old_sigint = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        # Retry logic matching NoDriverBackend._start_async()
        max_attempts = 3
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            # The block registers each Chrome with the SIGTERM and SIGHUP
            # handlers as soon as it is spawned, until the handle below or the
            # end of a failed attempt takes over.
            with chrome_in_own_session() as launch:
                try:
                    browser = await nodriver.start(
                        headless=headless,
                        sandbox=False,
                        browser_args=base_browser_args(),
                    )
                except Exception as exc:
                    # A failed attempt's Chrome is still running ("Failed to
                    # connect" is raised after the spawn); end it before the
                    # next one starts.
                    await launch.end_launched()
                    last_exc = exc
                    if "Failed to connect to browser" not in str(exc):
                        raise
                else:
                    handle = NodriverBrowserHandle(browser)
                    signals.register_live_browser(handle)
                    break
            if attempt < max_attempts:
                LOG.warning(
                    "observe_browser_connect_retry",
                    attempt=attempt,
                    max_attempts=max_attempts,
                )
                await asyncio.sleep(1.0 * attempt)
        else:
            raise RuntimeError(browser_connect_failure_message(max_attempts)) from last_exc
    finally:
        signal.signal(signal.SIGINT, old_sigint)

    try:
        own_session = check_chrome_in_own_session(browser)
        tab = browser.main_tab

        if session is not None:
            injected, filtered = await inject_cookies_to_nodriver(tab, session.cookies)
            msg = f"[dim]Injected {injected} cookie(s)"
            if filtered:
                msg += f" ({filtered} bot-detection cookie(s) filtered)"
            msg += "[/dim]"
            console.print(msg)

        run_id = make_run_id()
        # The run dir is keyed by the slugified session name, the same
        # mapping every observe reader applies (#151).
        storage = ObserveStorage(OBSERVE_BASE_DIR, session_dirname(namespace), run_id)
        bodies_dir = storage.run_dir / "bodies"
        backend = NodriverCaptureBackend(
            browser,
            get_tab=lambda: tab,
            bodies_dir=bodies_dir,
            max_body_size=max_body_size,
            # The page's DOM at each credential-shaped POST: a login modal a script
            # renders is in no recorded HTML response (#267).
            dom_snapshots_dir=storage.run_dir / "snapshots",
            failure_step=failure_step,
        )
        await backend.start_capture_async()

        tab = await browser.get(url)
        return ObserveSession(
            browser=browser,
            handle=handle,
            tab=tab,
            storage=storage,
            backend=backend,
            own_session=own_session,
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


# The throwaway the prompt offers: generated, so it is no word on any page and no
# field name, and never the real password with a typo.
_THROWAWAY_ALPHABET = string.ascii_lowercase + string.digits


def _throwaway_password() -> str:
    """A wrong password for the step: ``wrong-`` and 6 random letters and digits."""
    return "wrong-" + "".join(secrets.choice(_THROWAWAY_ALPHABET) for _ in range(6))


def _step_instruction(throwaway: str) -> str:
    return (
        "\n[bold]Recording.[/bold] First, a wrong password: in the browser, submit the "
        f"login with this throwaway wrong password, [bold]{escape(throwaway)}[/bold] (never "
        "your real one with a typo), wait for the error to show, then press Enter here. "
        "Then log in for real, and press Ctrl+C here to stop and save."
    )


async def _wrong_password_step(
    backend: Any, stop_event: asyncio.Event, loop: asyncio.AbstractEventLoop, stdin: Any
) -> None:
    """Wait for Enter on *stdin* (a terminal) and mark the last credential post as the
    failed one (the backend's ``mark_failed_login``), or for *stop_event*. Enter before
    any credential post asks again. The prompt offers a generated throwaway
    (:func:`_throwaway_password`), so it collides with no word on the page; whatever
    the user types is accepted. End of input (Ctrl+D) skips the step and tells the
    backend so (``skip_failure_step``), which then holds and reads nothing more for it.
    The loop watches *stdin* with ``add_reader`` until the step ends, so the SIGINT
    handler keeps working while it waits; nothing reads stdin after."""
    fd = stdin.fileno()
    chunks: asyncio.Queue[bytes] = asyncio.Queue()

    def readable() -> None:
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            chunk = b""
        chunks.put_nowait(chunk)

    console.print(_step_instruction(_throwaway_password()))
    loop.add_reader(fd, readable)
    try:
        stopped = asyncio.ensure_future(stop_event.wait())
        try:
            while True:
                line = asyncio.ensure_future(chunks.get())
                await asyncio.wait({line, stopped}, return_when=asyncio.FIRST_COMPLETED)
                if not line.done():
                    line.cancel()
                    return
                chunk = line.result()
                if not chunk:
                    backend.skip_failure_step()
                    console.print(
                        "[yellow]Input closed: the wrong-password step is skipped. Log in, "
                        "then press Ctrl+C here to stop and save.[/yellow]"
                    )
                    return
                if b"\n" not in chunk:
                    continue
                if backend.mark_failed_login():
                    console.print(
                        "[green]Marked the last login attempt as the wrong password.[/green] "
                        "Now log in for real, then press Ctrl+C here to stop and save."
                    )
                    return
                console.print(
                    "[yellow]No credential post yet: submit the wrong password in the "
                    "browser, wait for the error, then press Enter here again.[/yellow]"
                )
        finally:
            stopped.cancel()
    finally:
        loop.remove_reader(fd)


async def run_observe_interactive(
    namespace: str,
    url: str,
    max_body_size: int,
    *,
    session_name: str | None = None,
    wrong_password_first: bool = False,
    stdin: Any = None,
) -> None:
    """Async implementation of observe interactive.

    With *wrong_password_first*, the recording starts with the wrong-password step
    (:func:`_wrong_password_step`), which reads Enter on *stdin* (``sys.stdin`` when
    None): a stdin that is no terminal is refused before the browser opens, since
    nobody could press Enter in it. The recorder scrubs the failed post's password
    from that post's own body, and from a 307 or 308 resend of it, when it is marked
    (``mark_failed_login``) and again at stop, and when the mark kept no failure
    text, one line says so before the run is saved."""
    if wrong_password_first:
        stdin = sys.stdin if stdin is None else stdin
        if not stdin.isatty():
            console.print(
                "[red]--wrong-password-first needs a terminal to press Enter in, and "
                "stdin is not one.[/red]"
            )
            raise typer.Exit(1)

    observed = await setup_observe_session(
        namespace,
        url,
        max_body_size,
        headless=False,
        session_name=session_name,
        failure_step=wrong_password_first,
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

        try:
            if not observed.own_session:
                console.print(f"[yellow]{escape(CHROME_SHARES_SESSION_CONSEQUENCE)}[/yellow]")
            if wrong_password_first:
                await _wrong_password_step(observed.backend, stop_event, loop, stdin)
            elif not observed.own_session:
                console.print("\n[bold]Recording... press Ctrl+C to stop and save[/bold]\n")
            else:
                console.print("\n[bold]Recording... press Ctrl+C to stop and save[/bold]")
                console.print(
                    "[dim]Tip: Use Ctrl+C in this terminal for a complete capture "
                    "(screenshot, page source, and network data). "
                    "Closing the browser window will preserve network data only.[/dim]\n"
                )
            await stop_event.wait()
        finally:
            loop.remove_signal_handler(signal.SIGINT)

        console.print("\n[dim]Recording stopped. Saving capture...[/dim]")
        if wrong_password_first:
            # The reads after the marked post end here (save_observe_run would end
            # them too), so what they kept is known before the save.
            await observed.backend.close_after_reads()
            if observed.backend.marked_failure_candidates() == ():
                console.print(
                    "[yellow]No failure text was kept from the wrong password (the run "
                    "stopped before the page was read, or no text passed the checks): "
                    "the generated plugin's failure text is yours to fill in.[/yellow]"
                )
        await save_observe_run(
            observed.storage, observed.backend, "interactive-final", console=console
        )
    finally:
        stop_observe_browser(observed.browser, observed.handle)
