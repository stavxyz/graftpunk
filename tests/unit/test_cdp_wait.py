"""Bounded waits on nodriver DevTools commands never cancel the command
(``graftpunk.cdp_wait``), at every place graftpunk bounds one whose connection is
used again (#205 review sweep)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from graftpunk.cdp_wait import answer_by, answer_within
from graftpunk.tokens import Token
from tests.unit.cdp048 import Connection048

_LATE = 0.2  # when a slow command answers
_BOUND = 0.05  # the bound each site is given here, well before _LATE


@pytest.fixture
async def connection() -> AsyncIterator[Connection048]:
    conn = Connection048()
    yield conn
    conn.close()


@pytest.fixture
async def loop_errors() -> AsyncIterator[list[dict[str, Any]]]:
    """What the running loop's exception handler is given: a late answer nobody
    collected would show up here as "exception was never retrieved"."""
    errors: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    yield errors
    loop.set_exception_handler(previous)


async def _after_the_late_answer() -> None:
    await asyncio.sleep(_LATE + 0.1)


class TestTheModel:
    async def test_cancelling_a_command_ends_the_listener(self, connection: Connection048) -> None:
        """The control: what asyncio.wait_for and asyncio.timeout do on expiry."""
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(connection.send("late", after=_LATE), timeout=_BOUND)
        await _after_the_late_answer()

        assert connection.listener.done()
        assert not await connection.still_answers()


class TestAnswerBy:
    async def test_an_answer_in_time_is_returned(self, connection: Connection048) -> None:
        assert await answer_within(connection.send("now"), 1.0) == "now"

    async def test_a_late_answer_leaves_the_connection_working(
        self, connection: Connection048, loop_errors: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(TimeoutError):
            await answer_within(connection.send("late", after=_LATE), _BOUND)
        await _after_the_late_answer()

        assert await connection.still_answers()
        assert loop_errors == []

    async def test_a_late_error_is_collected(
        self, connection: Connection048, loop_errors: list[dict[str, Any]]
    ) -> None:
        with pytest.raises(TimeoutError):
            await answer_within(connection.send(RuntimeError("tab gone"), after=_LATE), _BOUND)
        await _after_the_late_answer()

        assert await connection.still_answers()
        assert loop_errors == []

    async def test_a_cancelled_caller_leaves_the_command_running(
        self, connection: Connection048
    ) -> None:
        waiter = asyncio.ensure_future(answer_within(connection.send("late", after=_LATE), 5.0))
        await asyncio.sleep(_BOUND)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        await _after_the_late_answer()

        assert await connection.still_answers()

    async def test_an_end_already_past_still_takes_an_answer_at_hand(self) -> None:
        async def at_hand() -> str:
            return "here"

        loop = asyncio.get_running_loop()
        assert await answer_by(at_hand(), end=loop.time() - 1.0) == "here"


class TestEverySiteLeavesTheConnectionWorking:
    """Each place that bounds a nodriver call whose connection is used again."""

    async def test_login_token_extraction(self, connection: Connection048) -> None:
        from graftpunk.tokens import extract_tokens_from_tab

        token = Token(name="X-CSRF", source="page", pattern=r'csrf="([^"]+)"', page_url="/p")
        tab = MagicMock()
        tab.browser.get = lambda url: connection.send(tab, after=_LATE)

        with patch("graftpunk.tokens._TOKEN_NAV_TIMEOUT", _BOUND):
            results = await extract_tokens_from_tab(tab, [token], "https://myshop.example")
        await _after_the_late_answer()

        assert results == {}
        assert await connection.still_answers()

    async def test_browser_token_extraction_reads_the_next_page(
        self, connection: Connection048
    ) -> None:
        """The first page's navigation answers after its bound (0.2 s), at 0.25 s,
        while the second page's navigation over the same connection is still
        waiting for its own answer (0.3 s); that answer still comes."""
        from graftpunk.tokens import _extract_tokens_browser

        slow = Token(name="X-Slow", source="page", pattern=r'a="([^"]+)"', page_url="/slow")
        fast = Token(name="X-Fast", source="page", pattern=r'b="([^"]+)"', page_url="/fast")
        fast_tab = MagicMock()
        fast_tab.get_content = lambda: connection.send('<meta b="2">')
        fast_tab.sleep = AsyncMock()

        async def fast_get(url: str) -> Any:
            await connection.send(None, after=0.1)
            return fast_tab

        browser = MagicMock()
        browser.main_tab = MagicMock()
        browser.tabs = []
        browser.get = lambda url: (
            connection.send(MagicMock(), after=0.25) if "/slow" in url else fast_get(url)
        )

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
            patch("graftpunk.tokens._deregister_nodriver_browser"),
            patch("graftpunk.tokens._TOKEN_NAV_TIMEOUT", 0.2),
        ):
            results = await _extract_tokens_browser(
                requests.Session(), [slow, fast], "https://myshop.example"
            )

        assert results == {"X-Fast": "2"}
        assert not connection.listener.done()

    async def test_login_page_navigation(self, connection: Connection048) -> None:
        from graftpunk.exceptions import PluginError
        from graftpunk.plugins.login_engine import generate_login_method
        from tests.unit.test_login_engine import DeclarativeHN, _make_nodriver_mock_bs

        mock_bs, instance = _make_nodriver_mock_bs()
        instance.driver = MagicMock()
        instance.driver.get = lambda url: connection.send(MagicMock(), after=_LATE)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.login_engine._LOGIN_NAV_TIMEOUT", _BOUND),
            pytest.raises(PluginError, match="Timed out loading login page"),
        ):
            await generate_login_method(DeclarativeHN())({"username": "u", "password": "p"})  # noqa: S106
        await _after_the_late_answer()

        assert await connection.still_answers()

    async def test_cookie_transfer(self, connection: Connection048) -> None:
        from graftpunk.exceptions import BrowserError
        from graftpunk.session import BrowserSession

        with patch.object(BrowserSession, "__init__", return_value=None):
            session = BrowserSession.__new__(BrowserSession)
            browser = MagicMock()
            browser.cookies.get_all = lambda: connection.send([], after=_LATE)
            session._backend_instance = MagicMock()
            session._backend_instance._browser = browser

            with pytest.raises(BrowserError, match="Cookie transfer timed out"):
                await session.transfer_nodriver_cookies_to_session(timeout=_BOUND)
        await _after_the_late_answer()

        assert await connection.still_answers()

    async def test_answering_a_dialog_on_a_nodriver_tab(self, connection: Connection048) -> None:
        from graftpunk.dialogs import DialogAnswerer

        tab = MagicMock()
        tab.send = lambda cmd, **_kwargs: connection.send(None, after=_LATE)
        answerer = DialogAnswerer(tab)
        event = MagicMock()
        event.type_.value = "alert"
        event.url = "https://myshop.example/"

        with patch("graftpunk.dialogs._DIALOG_ANSWER_WAIT_S", _BOUND):
            await answerer.on_dialog_opening(event)
        await _after_the_late_answer()

        assert answerer.failed == 1
        assert await connection.still_answers()
