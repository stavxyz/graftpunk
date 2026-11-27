"""A browser graftpunk starts with ``nodriver.start()`` is waited for and its
subprocess transport closed before the event loop that started it closes.

nodriver's ``Browser.stop()`` signals Chrome and returns. When the loop closed
before Chrome exited, the interpreter printed ``Loop ... that handles pid ...
is closed`` and ``Exception ignored in BaseSubprocessTransport.__del__ ...
RuntimeError: Event loop is closed`` after a command's token read and a
hand-written login (seen with Chrome 158 on 2026-11-27).

No Chrome: the stand-in is a Python child holding the three pipes nodriver
opens, which exits on SIGTERM.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import os
import sys
import time
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

pytestmark = pytest.mark.skipif(os.name != "posix", reason="SIGTERM ends the stand-in")

_STAND_IN = (
    "import signal, sys, time\n"
    "signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))\n"
    "print('ready', flush=True)\n"
    "time.sleep(30)\n"
)


class _Browser:
    """What graftpunk uses of a nodriver ``Browser``: ``stop()`` signals the
    process and does not wait, as nodriver 0.50.6's does."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self._process = process
        self.config = MagicMock(uses_custom_data_dir=True)
        self.tabs: list[Any] = []
        tab = MagicMock()
        tab.get_content = AsyncMock(return_value='csrf = "v1";')
        tab.sleep = AsyncMock()
        self.main_tab = tab
        self.get = AsyncMock(return_value=tab)

    def stop(self) -> None:
        self._process.terminate()


async def _stand_in() -> asyncio.subprocess.Process:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _STAND_IN,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    await proc.stdout.readline()
    return proc


def _closed_loop_noise(
    run: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> tuple[list[str], asyncio.subprocess.Process]:
    """Run *run* (a coroutine function taking the stand-in) under its own
    ``asyncio.run``, drop it, and return what the interpreter reported about
    a closed loop, with the stand-in's process."""
    unraisable: list[str] = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: unraisable.append(repr(u.exc_value)))
    procs: list[asyncio.subprocess.Process] = []

    async def main() -> None:
        proc = await _stand_in()
        procs.append(proc)
        await run(proc)

    with caplog.at_level(logging.WARNING, logger="asyncio"):
        asyncio.run(main())
        # The watcher thread reports an exit it collects after the loop closed.
        for _ in range(50):
            if procs[0].returncode is not None:
                break
            time.sleep(0.05)
        gc.collect()
    noise = unraisable + [r.getMessage() for r in caplog.records if "is closed" in r.getMessage()]
    return noise, procs[0]


def test_a_commands_token_browser_is_reaped_before_its_loop_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from graftpunk.tokens import Token, _extract_tokens_browser

    token = Token(name="X-CSRF", source="page", pattern=r'csrf = "([^"]+)"', page_url="/app")

    async def run(proc: asyncio.subprocess.Process) -> None:
        browser = _Browser(proc)
        with (
            patch("graftpunk.tokens.nodriver_start", AsyncMock(return_value=browser)),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
            patch("graftpunk.tokens.answer_dialogs", AsyncMock()),
            patch("graftpunk.tokens.answer_dialogs_in_new_tabs", AsyncMock()),
            patch("graftpunk.tokens.close_dialogs_in_new_tabs", AsyncMock()),
            patch("graftpunk.tokens.prepare_browser_launch", lambda: None),
        ):
            assert await _extract_tokens_browser(
                requests.Session(), [token], "https://myshop.example"
            ) == {"X-CSRF": "v1"}
        assert proc.returncode == 0  # waited for before the read returned

    noise, proc = _closed_loop_noise(run, monkeypatch, caplog)
    assert noise == []
    assert proc.returncode == 0


def test_an_observe_browser_is_reaped_before_its_loop_closes(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from graftpunk.cli.observe_browser import end_observe_browser

    async def run(proc: asyncio.subprocess.Process) -> None:
        with patch("graftpunk.cli.observe_browser.close_dialogs_in_new_tabs", AsyncMock()):
            await end_observe_browser(_Browser(proc), MagicMock())
        assert proc.returncode == 0

    noise, proc = _closed_loop_noise(run, monkeypatch, caplog)
    assert noise == []
    assert proc.returncode == 0


@pytest.mark.parametrize("where", ["graftpunk.cli.observe_browser", "graftpunk.tokens"])
def test_chrome_has_exited_before_its_temp_profile_is_removed(
    where: str, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Removing the profile of a Chrome still running races its writes to it."""
    from graftpunk.cli.observe_browser import end_observe_browser
    from graftpunk.tokens import Token, _extract_tokens_browser

    at_removal: list[int | None] = []

    async def run(proc: asyncio.subprocess.Process) -> None:
        browser = _Browser(proc)
        with patch(
            f"{where}.remove_browser_temp_profile",
            lambda _browser: at_removal.append(proc.returncode),
        ):
            if where == "graftpunk.tokens":
                token = Token(name="X", source="page", pattern=r'csrf = "([^"]+)"')
                with (
                    patch("graftpunk.tokens.nodriver_start", AsyncMock(return_value=browser)),
                    patch(
                        "graftpunk.session.inject_cookies_to_nodriver",
                        new_callable=AsyncMock,
                        return_value=(0, 0),
                    ),
                    patch("graftpunk.tokens.answer_dialogs", AsyncMock()),
                    patch("graftpunk.tokens.answer_dialogs_in_new_tabs", AsyncMock()),
                    patch("graftpunk.tokens.close_dialogs_in_new_tabs", AsyncMock()),
                    patch("graftpunk.tokens.prepare_browser_launch", lambda: None),
                ):
                    await _extract_tokens_browser(requests.Session(), [token], "https://x.example")
            else:
                with patch("graftpunk.cli.observe_browser.close_dialogs_in_new_tabs", AsyncMock()):
                    await end_observe_browser(browser, MagicMock())

    _noise, _proc = _closed_loop_noise(run, monkeypatch, caplog)
    assert at_removal == [0]


def test_reaping_nothing_is_a_no_op() -> None:
    from graftpunk.backends.nodriver import reap_stopped_chrome

    asyncio.run(reap_stopped_chrome(None))


def test_a_hand_written_logins_chrome_has_exited_when_its_block_ends(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``browser_session()`` ends Chrome through the session's own stop, which
    waits for the exit, so the process is gone when the ``async with`` ends."""
    from graftpunk.plugins.cli_plugin import SitePlugin
    from graftpunk.session import BrowserSession

    class Shop(SitePlugin):
        site_name = "myshop"
        session_name = "myshop"
        help_text = "Test"
        base_url = "https://myshop.example"

    at_exit: list[int | None] = []

    async def run(proc: asyncio.subprocess.Process) -> None:
        browser = _Browser(proc)
        session = BrowserSession(backend="nodriver", headless=False)
        backend = session._backend_instance

        async def start() -> None:
            backend._started = True
            backend._browser = browser

        session.start_async = start  # type: ignore[method-assign]
        session.transfer_nodriver_cookies_to_session = AsyncMock()  # type: ignore[method-assign]

        async def page_url(_tab: Any) -> Any:
            return MagicMock(live=True, url="https://myshop.example/account")

        with (
            patch("graftpunk.BrowserSession", return_value=session),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                side_effect=RuntimeError("no capture in this test"),
            ),
            patch("graftpunk.plugins.login_settle.page_url_nodriver", page_url),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.backends.nodriver.close_dialogs_in_new_tabs", AsyncMock()),
        ):
            async with Shop().browser_session():
                assert proc.returncode is None
        at_exit.append(proc.returncode)

    _noise, _proc = _closed_loop_noise(run, monkeypatch, caplog)
    assert at_exit == [0]
