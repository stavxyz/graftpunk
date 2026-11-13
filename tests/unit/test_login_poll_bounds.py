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


class _SlowTab:
    """A tab that answers its URL at once and its document only after *read_seconds*."""

    def __init__(self, read_seconds: float) -> None:
        self.read_seconds = read_seconds
        self.reads_started = 0
        self.url = "https://app.example.com/login"

    async def get_content(self) -> str:
        self.reads_started += 1
        await asyncio.sleep(self.read_seconds)
        return "<html>Signing in</html>"

    async def query_selector(self, selector: str) -> None:
        return None


_SLACK = 0.3  # scheduling slack on a loaded machine, on top of the documented bound


class TestNodriverDeadline:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("read_seconds", [0.8, 3600.0])
    async def test_the_signal_poll_ends_within_one_interval_of_its_timeout(
        self, read_seconds: float
    ) -> None:
        """A read slower than the time left used to run to completion, and the
        deadline tick then read again; a read that never returned held the login
        forever."""
        loop = asyncio.get_running_loop()
        tab = _SlowTab(read_seconds)
        timeout = 1.0
        started = loop.time()

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result = await asyncio.wait_for(
                _wait_for_login_signal_nodriver(
                    tab=tab,
                    login_config=DeclarativeNodriverLongWait.login_config,
                    failure_text="Invalid credentials",
                    site_name="ndlong",
                    deadline=started + timeout,
                    pre_submit_url="https://app.example.com/login",
                ),
                timeout=30,
            )
        elapsed = loop.time() - started

        assert result is False
        assert elapsed <= timeout + _LOGIN_POLL_INTERVAL + _SLACK, elapsed
        warning = _warning_kwargs(mock_log, "login_signal_timeout")
        assert "did not answer within" in warning["error"]

    @pytest.mark.asyncio
    async def test_the_grace_window_ends_within_one_interval_of_its_end(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _SlowTab(3600.0)
        window = 1.0
        started = loop.time()

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result = await asyncio.wait_for(
                _watch_for_failure_nodriver(
                    tab=tab,
                    failure_text="Invalid credentials",
                    site_name="ndlong",
                    deadline=started + window,
                ),
                timeout=30,
            )
        elapsed = loop.time() - started

        assert result is False
        assert elapsed <= window + _LOGIN_POLL_INTERVAL + _SLACK, elapsed
        assert (
            "did not answer within" in _warning_kwargs(mock_log, "login_page_unreadable")["error"]
        )

    @pytest.mark.asyncio
    async def test_a_read_that_answers_in_time_is_not_cut_off(self) -> None:
        loop = asyncio.get_running_loop()
        tab = _SlowTab(0.0)
        tab.get_content = _content_with("Invalid credentials")  # type: ignore[method-assign]

        with patch("graftpunk.plugins.login_settle.LOG") as mock_log:
            result = await _wait_for_login_signal_nodriver(
                tab=tab,
                login_config=DeclarativeNodriverLongWait.login_config,
                failure_text="Invalid credentials",
                site_name="ndlong",
                deadline=loop.time() + 5.0,
                pre_submit_url="https://app.example.com/login",
            )

        assert result is False
        assert _warning_kwargs(mock_log, "login_failure_text_detected")["plugin"] == "ndlong"


def _content_with(text: str) -> Any:
    async def get_content() -> str:
        return f"<html>{text}</html>"

    return get_content


def test_a_window_with_no_tick_reports_no_invented_reason() -> None:
    """The default reason "neither a page nor a URL" could never be reported: every
    wait reads the document on its last tick (#205). A window starts with none."""
    assert _ReadWindow().error == ""


@pytest.mark.asyncio
async def test_a_tick_that_wakes_past_the_deadline_is_read_not_cut_off() -> None:
    """A busy loop can wake a tick after the deadline; that tick is the deadline
    tick, so an instant read is taken and ends the wait, not cut off for having
    no time left."""
    from graftpunk.plugins import login_settle

    real_sleep = asyncio.sleep
    loop = asyncio.get_running_loop()
    tab = _SlowTab(0.0)
    reads = []

    async def instant_content() -> str:
        reads.append(loop.time())
        return "<html>Signing in</html>"

    tab.get_content = instant_content  # type: ignore[method-assign]

    async def late_sleep(seconds: float) -> None:
        await real_sleep(seconds + 0.7)

    with (
        patch.object(login_settle.asyncio, "sleep", late_sleep),
        patch("graftpunk.plugins.login_settle.LOG") as mock_log,
    ):
        result = await _wait_for_login_signal_nodriver(
            tab=tab,
            login_config=DeclarativeNodriverLongWait.login_config,
            failure_text="Invalid credentials",
            site_name="ndlong",
            deadline=loop.time() + 1.0,
            pre_submit_url="https://app.example.com/login",
        )

    assert result is False
    assert len(reads) == 1
    read_failures = [
        c for c in mock_log.debug.call_args_list if c[0][0] == "login_tick_read_failed"
    ]
    assert read_failures == []
