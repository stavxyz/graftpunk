"""The post-submit login wait's edges (#205): a malformed ``success`` selector on
nodriver, a selenium read that fails with something other than a
WebDriverException, and the deadline the nodriver wait keeps however slowly the
browser answers."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import patch

import pytest

from graftpunk.exceptions import PluginError
from graftpunk.plugins import SitePlugin
from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep
from graftpunk.plugins.login_settle import (
    _LOGIN_POLL_INTERVAL,
    _ReadWindow,
    _wait_for_login_signal_nodriver,
    _watch_for_failure_nodriver,
)
from tests.unit.test_login_engine import (
    _LOGIN_CREDENTIALS,
    DeclarativeSeleniumPoll,
    _nodriver_session_for,
    _ScriptedNodriverTab,
    _ScriptedSeleniumDriver,
    _selenium_session_for,
    _warning_kwargs,
)

# What nodriver 0.48.1 raises for a failed DOM.querySelector: the CDP error's
# generic server-error code and a message that does not say "syntax".
_GENERIC_CDP_ERROR = {"code": -32000, "message": "DOM Error while querying"}


class DeclarativeNodriverLongWait(SitePlugin):
    """A nodriver plugin whose budget dwarfs one poll interval, so a login that ends
    at once is told apart from one that ends at its timeout."""

    site_name = "ndlong"
    session_name = "ndlong"
    help_text = "ND Long"
    base_url = "https://example.com"
    backend = "nodriver"
    login_config = LoginConfig(
        steps=[LoginStep(fields={"username": "#user", "password": "#pass"}, submit="#submit")],
        url="/login",
        failure="Invalid credentials",
        success=".dashboard",
        timeout=30.0,
        settle=0.0,
    )


class _SelectorErrorTab(_ScriptedNodriverTab):
    """A tab whose success-element query raises the generic CDP error, and whose page
    answers the selector check as *rejects*."""

    def __init__(self, *, rejects: bool, raise_on_ticks: tuple[int, ...], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._rejects = rejects
        self._raise_on_ticks = raise_on_ticks
        self.selector_checks: list[str] = []
        self.clicked_at: float | None = None

    async def query_selector(self, selector: str) -> Any:
        from nodriver.core.connection import ProtocolException

        if selector == self._success_selector and self.tick in self._raise_on_ticks:
            self.order.append("query")
            raise ProtocolException(dict(_GENERIC_CDP_ERROR))
        element = await super().query_selector(selector)
        if selector == "#submit":
            click = element.click

            async def timed_click() -> None:
                self.clicked_at = time.monotonic()
                await click()

            element.click = timed_click
        return element

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        if "graftpunk:selector-check" in expression:
            self.selector_checks.append(expression)
            # nodriver hands back a true value as itself and a false one as the
            # RemoteObject, which is not True.
            return True if self._rejects else object()
        return await super().evaluate(expression, **kwargs)


class TestNodriverMalformedSelector:
    @pytest.mark.asyncio
    async def test_a_selector_the_page_rejects_ends_the_login_at_once_naming_it(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _SelectorErrorTab(
            rejects=True, raise_on_ticks=tuple(range(1000)), success_selector=".dashboard"
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            pytest.raises(PluginError) as raised,
        ):
            await generate_login_method(DeclarativeNodriverLongWait())(_LOGIN_CREDENTIALS)
        assert tab.clicked_at is not None
        after_submit = time.monotonic() - tab.clicked_at

        assert "'.dashboard'" in str(raised.value)
        assert "not a valid CSS selector" in str(raised.value)
        # The first poll tick, one interval after the submit, not the 30-second timeout.
        assert after_submit < 4 * _LOGIN_POLL_INTERVAL, after_submit
        assert tab.order.count("query") == 1
        assert len(tab.selector_checks) == 1
        assert '".dashboard"' in tab.selector_checks[0]
        assert "cookies" not in tab.events

    @pytest.mark.asyncio
    async def test_a_valid_selector_whose_query_fails_mid_navigation_keeps_waiting(
        self,
    ) -> None:
        """The same exception from a node that went away is a pending tick, as before."""
        from graftpunk.plugins.login_engine import generate_login_method

        tab = _SelectorErrorTab(
            rejects=False, raise_on_ticks=(1,), success_selector=".dashboard", success_from=2
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverLongWait())(_LOGIN_CREDENTIALS)

        assert result is True
        assert len(tab.selector_checks) == 1
        assert "cookies" in tab.events

    @pytest.mark.asyncio
    async def test_a_selector_check_that_cannot_run_leaves_the_tick_pending(self) -> None:
        from graftpunk.plugins.login_engine import generate_login_method

        class _CheckFails(_SelectorErrorTab):
            async def evaluate(self, expression: str, **kwargs: Any) -> Any:
                if "graftpunk:selector-check" in expression:
                    self.selector_checks.append(expression)
                    raise RuntimeError("no close frame received or sent")
                return await super().evaluate(expression, **kwargs)

        tab = _CheckFails(
            rejects=True, raise_on_ticks=(1,), success_selector=".dashboard", success_from=2
        )
        mock_bs, _instance = _nodriver_session_for(tab)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
        ):
            result = await generate_login_method(DeclarativeNodriverLongWait())(_LOGIN_CREDENTIALS)

        assert result is True
        assert len(tab.selector_checks) == 1


class TestSeleniumReadGuard:
    def test_a_driver_whose_chromedriver_is_gone_fails_the_login_cleanly(self) -> None:
        """selenium 4.40.0 raises urllib3's MaxRetryError from page_source when nothing
        listens on chromedriver's port; it used to escape login()."""
        from urllib3.exceptions import MaxRetryError

        from graftpunk.plugins.login_engine import generate_login_method

        class _ChromedriverGone(_ScriptedSeleniumDriver):
            @property
            def page_source(self) -> str:
                raise MaxRetryError(None, "/session/dead/source", reason="Connection refused")  # type: ignore[arg-type]

        driver = _ChromedriverGone(success_selector=".dashboard", urls=("",))
        mock_bs, _instance = _selenium_session_for(driver)

        with (
            patch("graftpunk.BrowserSession", mock_bs),
            patch("graftpunk.plugins.cli_plugin.cache_session"),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result = generate_login_method(DeclarativeSeleniumPoll())(_LOGIN_CREDENTIALS)

        assert result is False
        assert "cookies" not in driver.events
        assert "Max retries exceeded" in _warning_kwargs(mock_log, "login_page_unreadable")["error"]
        read_failures = [
            c for c in mock_log.debug.call_args_list if c[0][0] == "login_tick_read_failed"
        ]
        assert read_failures
        assert {c[1]["exc_type"] for c in read_failures} == {"MaxRetryError"}


class _VirtualSelector:
    """A selector that, when nothing is ready, moves the loop's clock to the next
    timer instead of sleeping, so a test measures the wait's own arithmetic and
    never the machine's load."""

    def __init__(self, real: Any, loop: _VirtualTimeLoop) -> None:
        self._real = real
        self._loop = loop

    def select(self, timeout: float | None = None) -> Any:
        events = self._real.select(0)
        if not events:
            if timeout is None:
                raise AssertionError("the loop would wait forever: nothing is scheduled")
            self._loop.now += timeout
        return events

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock moves only when every task is waiting on a timer."""

    def __init__(self) -> None:
        super().__init__()
        self.now = 1000.0
        self._selector = _VirtualSelector(self._selector, self)

    def time(self) -> float:
        return self.now


def _run_virtual(make: Any) -> tuple[Any, float]:
    """Run ``make()`` (a coroutine factory) on a virtual-time loop; return its
    result and the virtual seconds it took."""
    loop = _VirtualTimeLoop()
    try:

        async def timed() -> tuple[Any, float]:
            started = loop.time()
            result = await make()
            return result, loop.time() - started

        return loop.run_until_complete(timed())
    finally:
        # As asyncio.run does: an abandoned read, or the model connection's
        # listener, is still pending here.
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


_FOREVER = 10_000.0  # a read that never answers within any wait


class _TimedTab:
    """A tab whose reads take a set time on the loop's clock. ``success_from`` is the
    loop time from which the success element is found, and ``evaluate`` answers the
    readiness read after ``ready_seconds``."""

    def __init__(
        self,
        *,
        read_seconds: float,
        success_from: float = float("inf"),
        ready_seconds: float = 0.0,
        page_seconds: float | None = None,
        page: str = "<html>Signing in</html>",
    ) -> None:
        self.read_seconds = read_seconds
        self.page_seconds = read_seconds if page_seconds is None else page_seconds
        self.success_from = success_from
        self.ready_seconds = ready_seconds
        self.page = page
        self.url = "https://app.example.com/login"
        self.readiness_reads = 0

    async def get_content(self) -> str:
        await asyncio.sleep(self.page_seconds)
        return self.page

    async def query_selector(self, selector: str) -> object | None:
        await asyncio.sleep(self.read_seconds)
        return object() if asyncio.get_running_loop().time() >= self.success_from else None

    async def evaluate(self, expression: str, **kwargs: Any) -> Any:
        assert expression == "document.readyState", expression
        self.readiness_reads += 1
        await asyncio.sleep(self.ready_seconds)
        return "complete"


def _config(timeout: float, *, failure: str | None = "Invalid credentials") -> LoginConfig:
    return LoginConfig(
        steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
        url="/login",
        failure=failure,
        success=".dashboard",
        timeout=timeout,
        settle=0.0,
    )


_TIMEOUTS = [0.6, 1.0, 2.0]
# The documented bound, with room only for float rounding.
_EPSILON = 1e-6


class TestNodriverDeadline:
    """The nodriver post-submit wait ends at most one poll interval past ``timeout``
    however slowly the browser answers (#205), measured on a virtual clock."""

    @pytest.mark.parametrize("timeout", _TIMEOUTS)
    @pytest.mark.parametrize("read_seconds", [0.2, 0.45, 0.8, _FOREVER])
    def test_the_signal_poll_ends_by_one_interval_past_its_timeout(
        self, timeout: float, read_seconds: float
    ) -> None:
        tab = _TimedTab(read_seconds=read_seconds)

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            return await _wait_for_login_signal_nodriver(
                tab=tab,
                login_config=_config(timeout),
                failure_text="Invalid credentials",
                site_name="ndlong",
                deadline=loop.time() + timeout,
                pre_submit_url="https://app.example.com/login",
            )

        with patch("graftpunk.plugins.login_settle.LOG"):
            result, elapsed = _run_virtual(wait)

        assert result is False
        assert elapsed <= timeout + _LOGIN_POLL_INTERVAL + _EPSILON, elapsed

    @pytest.mark.parametrize("timeout", _TIMEOUTS)
    def test_a_signal_found_at_the_deadline_gets_no_readiness_wait(self, timeout: float) -> None:
        """The reviewers' case: a slow read finds the success element on the deadline
        tick, and the readiness read never answers. The readiness wait used to add a
        second interval."""
        from graftpunk.plugins.login_settle import wait_for_login_outcome_nodriver

        holder: dict[str, _TimedTab] = {}

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            # The element lookup takes 0.4 s; the document comes back at once.
            tab = _TimedTab(
                read_seconds=0.4,
                page_seconds=0.0,
                success_from=loop.time() + timeout,
                ready_seconds=_FOREVER,
            )
            holder["tab"] = tab
            return await wait_for_login_outcome_nodriver(
                tab=tab,
                login_config=_config(timeout, failure=None),
                failure_text="",
                site_name="ndlong",
                pre_submit_url="https://app.example.com/login",
            )

        with patch("graftpunk.plugins.login_settle.LOG"):
            result, elapsed = _run_virtual(wait)

        assert result is True
        assert elapsed <= timeout + _LOGIN_POLL_INTERVAL + _EPSILON, elapsed
        assert holder["tab"].readiness_reads == 0

    @pytest.mark.parametrize("timeout", _TIMEOUTS)
    def test_a_readiness_read_that_never_answers_ends_with_the_wait(self, timeout: float) -> None:
        from graftpunk.plugins.login_settle import wait_for_login_outcome_nodriver

        holder: dict[str, _TimedTab] = {}

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            # Found on the first tick, half an interval in; readiness never answers.
            tab = _TimedTab(read_seconds=0.0, success_from=loop.time(), ready_seconds=_FOREVER)
            holder["tab"] = tab
            return await wait_for_login_outcome_nodriver(
                tab=tab,
                login_config=_config(timeout, failure=None),
                failure_text="",
                site_name="ndlong",
                pre_submit_url="https://app.example.com/login",
            )

        with patch("graftpunk.plugins.login_settle.LOG"):
            result, elapsed = _run_virtual(wait)

        assert result is True
        assert holder["tab"].readiness_reads == 1
        assert elapsed == pytest.approx(timeout + _LOGIN_POLL_INTERVAL), elapsed

    @pytest.mark.parametrize("timeout", _TIMEOUTS)
    def test_the_grace_window_ends_by_one_interval_past_its_end(self, timeout: float) -> None:
        tab = _TimedTab(read_seconds=_FOREVER)

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            return await _watch_for_failure_nodriver(
                tab=tab,
                failure_text="Invalid credentials",
                site_name="ndlong",
                deadline=loop.time() + timeout,
            )

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result, elapsed = _run_virtual(wait)

        assert result is False
        assert elapsed <= timeout + _LOGIN_POLL_INTERVAL + _EPSILON, elapsed
        assert (
            "did not answer before" in _warning_kwargs(mock_log, "login_page_unreadable")["error"]
        )

    def test_a_tick_that_wakes_just_before_the_deadline_still_reads(self) -> None:
        """A tick that wakes a few milliseconds before the deadline used to get a
        budget of those milliseconds, which cut off a 50 ms read (#205 review)."""
        tab = _TimedTab(read_seconds=0.05)

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            # The first tick wakes at 0.50, 0.03 before the deadline, and its read
            # takes 0.05.
            return await _wait_for_login_signal_nodriver(
                tab=tab,
                login_config=_config(0.53),
                failure_text="Invalid credentials",
                site_name="ndlong",
                deadline=loop.time() + 0.53,
                pre_submit_url="https://app.example.com/login",
            )

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            _run_virtual(wait)

        failures = [c for c in mock_log.debug.call_args_list if c[0][0] == "login_tick_read_failed"]
        assert failures == []

    def test_a_tick_that_wakes_past_the_deadline_is_the_last(self) -> None:
        """A busy loop can wake a tick after the deadline; it reads the page and the
        wait ends there."""
        from graftpunk.plugins import login_settle

        reads: list[float] = []

        class _CountingTab(_TimedTab):
            """Answers without sleeping: the patched sleep below would delay it too."""

            async def get_content(self) -> str:
                reads.append(asyncio.get_running_loop().time())
                return self.page

            async def query_selector(self, selector: str) -> None:
                return None

        tab = _CountingTab(read_seconds=0.0)
        real_sleep = asyncio.sleep

        async def late_sleep(seconds: float) -> None:
            await real_sleep(seconds + 0.7)

        async def wait() -> bool:
            loop = asyncio.get_running_loop()
            return await _wait_for_login_signal_nodriver(
                tab=tab,
                login_config=_config(1.0),
                failure_text="Invalid credentials",
                site_name="ndlong",
                deadline=loop.time() + 1.0,
                pre_submit_url="https://app.example.com/login",
            )

        with (
            patch.object(login_settle.asyncio, "sleep", late_sleep),
            patch("graftpunk.plugins.login_settle.LOG") as mock_log,
        ):
            result, _elapsed = _run_virtual(wait)

        assert result is False
        assert len(reads) == 1
        failures = [c for c in mock_log.debug.call_args_list if c[0][0] == "login_tick_read_failed"]
        assert failures == []


class _Connection048:
    """The part of nodriver 0.48.1's ``Connection`` that decides what an abandoned
    command does: ``send`` registers a future and awaits it, and the listener task
    sets each answer's result with no check that the future is still pending, so an
    answer to a cancelled command raises ``InvalidStateError`` there and ends the
    listener (``nodriver/core/connection.py``, 0.48.1)."""

    def __init__(self) -> None:
        self._futures: dict[int, asyncio.Future[str]] = {}
        self._answers: asyncio.Queue[tuple[int, str]] = asyncio.Queue()
        self._ids = 0
        self.listener = asyncio.ensure_future(self._listen())

    async def send(self, answer: str, *, after: float) -> str:
        loop = asyncio.get_running_loop()
        self._ids += 1
        future: asyncio.Future[str] = loop.create_future()
        self._futures[self._ids] = future
        loop.call_later(after, self._answers.put_nowait, (self._ids, answer))
        return await future

    async def _listen(self) -> None:
        while True:
            command_id, answer = await self._answers.get()
            self._futures.pop(command_id).set_result(answer)


class _ConnectionTab:
    """A tab whose document read goes over a ``_Connection048``; the first read
    answers after 0.3 s, later ones at once."""

    url = "https://app.example.com/login"

    def __init__(self, connection: _Connection048) -> None:
        self.connection = connection
        self.reads = 0

    async def get_content(self) -> str:
        self.reads += 1
        return await self.connection.send(
            "<html>Signing in</html>", after=0.3 if self.reads == 1 else 0.0
        )


class TestAnAbandonedReadLeavesTheTabWorking:
    def test_the_model_reproduces_the_listener_dying_on_a_cancelled_command(self) -> None:
        """The control: cancelling the command, as asyncio.wait_for does, ends the
        listener, so the next command is never answered."""

        async def scenario() -> tuple[bool, str]:
            connection = _Connection048()
            tab = _ConnectionTab(connection)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(tab.get_content(), timeout=0.1)
            await asyncio.sleep(0.5)
            try:
                second = await asyncio.wait_for(tab.get_content(), timeout=2.0)
            except TimeoutError:
                second = "no answer"
            return connection.listener.done(), second

        (listener_done, second), _ = _run_virtual(scenario)

        assert listener_done is True
        assert second == "no answer"

    def test_a_read_abandoned_by_the_wait_does_not_end_the_listener(self) -> None:
        from graftpunk.plugins.login_settle import _read_login_tick_nodriver_by

        async def scenario() -> tuple[str, bool, str]:
            loop = asyncio.get_running_loop()
            connection = _Connection048()
            tab = _ConnectionTab(connection)
            first = await _read_login_tick_nodriver_by(
                tab,
                end=loop.time() + 0.1,
                url=tab.url,
                success_selector="",
                want_page_text=True,
                site_name="ndlong",
            )
            await asyncio.sleep(0.5)
            second = await _read_login_tick_nodriver_by(
                tab,
                end=loop.time() + 2.0,
                url=tab.url,
                success_selector="",
                want_page_text=True,
                site_name="ndlong",
            )
            return first.error, connection.listener.done(), second.page_text

        with patch("graftpunk.plugins.login_settle.LOG"):
            (first_error, listener_done, second_page), _ = _run_virtual(scenario)

        assert "did not answer before" in first_error
        assert listener_done is False
        assert second_page == "<html>Signing in</html>"


def test_a_window_with_no_tick_reports_no_invented_reason() -> None:
    """The default reason "neither a page nor a URL" could never be reported: every
    wait reads the document on its last tick (#205). A window starts with none."""
    assert _ReadWindow().error == ""
