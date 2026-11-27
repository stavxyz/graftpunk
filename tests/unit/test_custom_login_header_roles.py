"""A hand-written login() through SitePlugin.browser_session() or
browser_session_sync() caches the browser's header roles, as a generated login
does (#401).

No Chrome: the nodriver tab and the Selenium driver are doubles that report the
requests a browser would, and the capture that reads them is graftpunk's own.
"""

from __future__ import annotations

import inspect
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from graftpunk.plugins.cli_plugin import SitePlugin

UA = "Mozilla/5.0 (Macintosh) Chrome/150.0.0.0 Safari/537.36"
SITE = "https://myshop.example"
# What Chrome reports for a page load: the renderer's headers on
# requestWillBeSent hold no Accept and no Sec-Fetch-*, and only the headers sent,
# on requestWillBeSentExtraInfo, say it is a navigation.
RENDERER_HEADERS = {
    "User-Agent": UA,
    "sec-ch-ua": '"Chromium";v="150"',
    "Upgrade-Insecure-Requests": "1",
}
BROWSER_HEADERS = {
    **RENDERER_HEADERS,
    "Sec-Fetch-Mode": "navigate",
    "Accept": "text/html",
    "Host": "myshop.example",
}


class _Shop(SitePlugin):
    site_name = "myshop"
    session_name = "myshop"
    help_text = "Test"
    base_url = SITE


class _Tab:
    """A nodriver tab double: handlers are registered as nodriver's are, and each
    get() reports the navigation's request to them as Chrome would."""

    def __init__(self) -> None:
        self.handlers: list[tuple[Any, Any]] = []
        self.target = SimpleNamespace(url=f"{SITE}/")
        self._next = 0
        self.sent: list[str] = []

    async def send(self, command: Any) -> None:
        # A nodriver CDP command is a generator whose first item is the request.
        self.sent.append(next(command)["method"])
        return None

    def add_handler(self, event_cls: Any, handler: Any) -> None:
        self.handlers.append((event_cls, handler))

    async def get(self, url: str) -> _Tab:
        import nodriver.cdp.network as network

        self._next += 1
        event = SimpleNamespace(
            request_id=str(self._next),
            request=SimpleNamespace(url=url, method="GET", headers=dict(RENDERER_HEADERS)),
            redirect_response=None,
            wall_time=1_700_000_000.0 + self._next,
            type_="Document",
            document_url=url,
        )
        sent = SimpleNamespace(request_id=str(self._next), headers=dict(BROWSER_HEADERS))
        for cls, cdp_event in (
            (network.RequestWillBeSent, event),
            (network.RequestWillBeSentExtraInfo, sent),
        ):
            for event_cls, handler in self.handlers:
                if event_cls is cls:
                    result = handler(cdp_event)
                    if inspect.isawaitable(result):
                        await result
        return self


class _NodriverSession:
    """What browser_session() uses of a nodriver BrowserSession."""

    def __init__(self, tab: _Tab) -> None:
        self.tab = tab
        self.driver = SimpleNamespace(get=self._open, stop=self._stop)
        self.current_url = ""
        self.transferred = False
        self.stopped = False

    def _stop(self) -> None:
        self.stopped = True

    async def start_async(self) -> None:
        return None

    async def _open(self, url: str) -> _Tab:
        return await self.tab.get(url)

    async def transfer_nodriver_cookies_to_session(self) -> None:
        self.transferred = True


def _page(url: str) -> Any:
    return SimpleNamespace(live=True, url=url)


async def _run_nodriver(login_body: Any) -> tuple[_NodriverSession, MagicMock]:
    tab = _Tab()
    session = _NodriverSession(tab)

    async def page_url(_tab: Any) -> Any:
        return _page(f"{SITE}/account")

    with (
        patch("graftpunk.BrowserSession", return_value=session),
        patch("graftpunk.plugins.login_settle.page_url_nodriver", page_url),
        patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
    ):
        async with _Shop().browser_session() as (yielded, yielded_tab):
            await login_body(yielded, yielded_tab)
    return session, cached


class TestNodriverBrowserSession:
    @pytest.mark.asyncio
    async def test_the_cached_session_carries_the_browsers_header_roles(self) -> None:
        async def login(_session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")

        session, cached = await _run_nodriver(login)

        stored = cached.call_args.args[0]
        assert stored is session
        assert stored._gp_header_roles == {
            "navigation": {k: v for k, v in BROWSER_HEADERS.items() if k != "Host"}
        }
        assert stored._gp_header_role_origins == {"navigation": SITE}
        assert session.transferred

    @pytest.mark.asyncio
    async def test_roles_a_plugin_set_itself_are_kept(self) -> None:
        mine = {"navigation": {"User-Agent": "pinned/1.0"}}

        async def login(session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")
            session._gp_header_roles = mine
            session._gp_header_role_origins = {"navigation": SITE}

        session, cached = await _run_nodriver(login)

        stored = cached.call_args.args[0]
        assert stored._gp_header_roles is mine
        assert stored._gp_header_role_origins == {"navigation": SITE}

    @pytest.mark.asyncio
    async def test_an_empty_value_a_plugin_set_is_replaced_by_the_capture(self) -> None:
        async def login(session: Any, tab: Any) -> None:
            session._gp_header_roles = {}
            await tab.get(f"{SITE}/login")

        _session, cached = await _run_nodriver(login)

        assert cached.call_args.args[0]._gp_header_roles["navigation"]["User-Agent"] == UA

    @pytest.mark.asyncio
    async def test_a_capture_that_found_no_roles_warns_once_and_the_login_caches(self) -> None:
        async def login(_session: Any, _tab: Any) -> None:
            return None

        with capture_logs() as logs:
            session, cached = await _run_nodriver(login)

        empty = [e for e in logs if e["event"] == "login_header_roles_empty"]
        assert len(empty) == 1
        assert empty[0]["log_level"] == "warning"
        assert empty[0]["requests"] == 0
        assert cached.call_args.args[0] is session
        assert session._gp_header_roles == {}

    @pytest.mark.asyncio
    async def test_a_capture_that_found_roles_does_not_warn(self) -> None:
        async def login(_session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")

        with capture_logs() as logs:
            await _run_nodriver(login)

        assert "login_header_roles_empty" not in [e["event"] for e in logs]

    @pytest.mark.asyncio
    async def test_a_login_that_raises_caches_nothing_and_stores_no_roles(self) -> None:
        async def login(_session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")
            raise ValueError("login failed")

        with pytest.raises(ValueError, match="login failed"):
            await _run_nodriver_raising(login)

    @pytest.mark.asyncio
    async def test_a_capture_that_cannot_start_does_not_fail_the_login(self) -> None:
        async def login(_session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")

        with patch(
            "graftpunk.observe.capture.create_capture_backend",
            side_effect=RuntimeError("no network domain"),
        ):
            session, cached = await _run_nodriver(login)

        stored = cached.call_args.args[0]
        assert stored is session
        assert "_gp_header_roles" not in vars(stored)

    @pytest.mark.asyncio
    async def test_a_capture_that_cannot_be_read_does_not_fail_the_login(self) -> None:
        async def login(_session: Any, tab: Any) -> None:
            await tab.get(f"{SITE}/login")

        capture = MagicMock()
        capture.start_capture_async = AsyncMock()
        capture.get_header_roles.side_effect = RuntimeError("unreadable")
        with (
            patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
            capture_logs() as logs,
        ):
            session, cached = await _run_nodriver(login)

        assert cached.call_args.args[0] is session
        assert "_gp_header_roles" not in vars(session)
        assert "login_header_capture_unavailable" in [e["event"] for e in logs]

    @pytest.mark.asyncio
    async def test_an_interrupt_while_the_capture_starts_still_stops_chrome(self) -> None:
        tab = _Tab()
        session = _NodriverSession(tab)
        capture = MagicMock()
        capture.start_capture_async = AsyncMock(side_effect=KeyboardInterrupt)
        with (
            patch("graftpunk.BrowserSession", return_value=session),
            patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            pytest.raises(KeyboardInterrupt),
        ):
            async with _Shop().browser_session():
                pass
        assert session.stopped
        cached.assert_not_called()


class TestCdpDomainsOnTheLoginTab:
    """Which DevTools domains each login's header capture enables on its tab."""

    @pytest.mark.asyncio
    async def test_a_hand_written_login_enables_the_network_domain_alone(self) -> None:
        tab_holder: list[_Tab] = []

        async def login(_session: Any, tab: Any) -> None:
            tab_holder.append(tab)
            await tab.get(f"{SITE}/login")

        session, _cached = await _run_nodriver(login)

        assert session.tab.sent == ["Network.enable"]
        assert session._gp_header_roles["navigation"]["User-Agent"] == UA

    @pytest.mark.asyncio
    async def test_the_generated_login_enables_network_and_runtime_as_in_1_45_0(self) -> None:
        from graftpunk.plugins.login_engine import _start_login_capture

        tab = _Tab()
        capture, storage = _start_login_capture(
            _Shop(), "nodriver", MagicMock(), "off", get_tab=lambda: tab
        )
        await capture.start_capture_async()

        assert storage is None
        assert tab.sent == ["Network.enable", "Runtime.enable"]


async def _run_nodriver_raising(login_body: Any) -> None:
    tab = _Tab()
    session = _NodriverSession(tab)

    async def page_url(_tab: Any) -> Any:
        return _page(f"{SITE}/account")

    with (
        patch("graftpunk.BrowserSession", return_value=session),
        patch("graftpunk.plugins.login_settle.page_url_nodriver", page_url),
        patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
    ):
        try:
            async with _Shop().browser_session() as (yielded, yielded_tab):
                await login_body(yielded, yielded_tab)
        finally:
            cached.assert_not_called()
            assert "_gp_header_roles" not in vars(session)
            assert not session.transferred


# ---------------------------------------------------------------------------
# Selenium: the roles come from Chrome's performance log, which ChromeDriver
# keeps only when the driver is launched asking for it.
# ---------------------------------------------------------------------------


def _perf(method: str, params: dict[str, Any]) -> dict[str, str]:
    return {"message": json.dumps({"message": {"method": method, "params": params}})}


class _Driver:
    """A WebDriver double that answers get_log("performance") as ChromeDriver
    does: with the navigations' events when launched with the performance log,
    else ``invalid argument: log type 'performance' not found``.

    It refuses the ``browser`` log type the same way, the worse of the two
    answers a ChromeDriver launched with ``goog:loggingPrefs`` holding only
    ``performance`` might give (which one it gives is unverified without a live
    ChromeDriver), so a capture that reads it would print a warning here."""

    def __init__(self, performance_log: bool) -> None:
        self.performance_log = performance_log
        self.visited: list[str] = []
        self.current_url = f"{SITE}/"
        self.quit = MagicMock()
        self.logs_read: list[str] = []

    def get(self, url: str) -> None:
        self.visited.append(url)
        self.current_url = url

    def get_log(self, kind: str) -> list[dict[str, str]]:
        import selenium.common.exceptions

        self.logs_read.append(kind)
        if kind != "performance":
            raise selenium.common.exceptions.InvalidArgumentException(
                f"invalid argument: log type '{kind}' not found"
            )
        if not self.performance_log:
            raise selenium.common.exceptions.InvalidArgumentException(
                "invalid argument: log type 'performance' not found"
            )
        events = []
        for i, url in enumerate(self.visited, start=1):
            events.append(
                _perf(
                    "Network.requestWillBeSent",
                    {
                        "requestId": str(i),
                        "wallTime": 1_700_000_000.0 + i,
                        "type": "Document",
                        "documentURL": url,
                        "request": {"url": url, "method": "GET", "headers": RENDERER_HEADERS},
                    },
                )
            )
            events.append(
                _perf(
                    "Network.requestWillBeSentExtraInfo",
                    {"requestId": str(i), "headers": BROWSER_HEADERS},
                )
            )
        return events

    def get_cookies(self) -> list[dict[str, Any]]:
        return []

    def execute_script(self, _script: str) -> str:
        return UA


def _stealth(made: list[_Driver]) -> Any:
    def create(
        *, headless: bool, performance_log: bool = False, answer_dialogs: bool = True
    ) -> _Driver:
        driver = _Driver(performance_log)
        made.append(driver)
        return driver

    return create


def _requests_init(self: Any, *_args: Any, **_kwargs: Any) -> None:
    # requestium's own __init__ would build a driver of its own.
    import requests

    requests.Session.__init__(self)


def _no_requestium() -> Any:
    import requestium

    return patch.object(requestium.Session, "__init__", _requests_init)


class TestSeleniumBrowserSessionSync:
    def test_the_cached_session_carries_the_browsers_header_roles(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        made: list[_Driver] = []
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            _Shop().browser_session_sync() as (_session, driver),
        ):
            driver.get(f"{SITE}/login")

        (driver,) = made
        assert driver.performance_log is True
        stored = cached.call_args.args[0]
        assert stored._gp_header_roles == {
            "navigation": {k: v for k, v in BROWSER_HEADERS.items() if k != "Host"}
        }
        assert stored._gp_header_role_origins == {"navigation": SITE}
        err = capsys.readouterr().err
        assert "Performance log collection failed" not in err
        assert "Browser log collection failed" not in err
        assert driver.logs_read == ["performance"]

    def test_a_capture_that_cannot_start_does_not_fail_the_login(self) -> None:
        made: list[_Driver] = []
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch(
                "graftpunk.observe.capture.create_capture_backend",
                side_effect=RuntimeError("no capture"),
            ),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            capture_logs() as logs,
            _Shop().browser_session_sync() as (session, driver),
        ):
            driver.get(f"{SITE}/login")

        assert cached.call_args.args[0] is session
        assert "_gp_header_roles" not in vars(session)
        assert "login_header_capture_unavailable" in [e["event"] for e in logs]

    def test_a_capture_that_cannot_be_drained_does_not_fail_the_login(self) -> None:
        made: list[_Driver] = []
        capture = MagicMock()
        capture.stop_capture.side_effect = RuntimeError("chromedriver went away")
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            capture_logs() as logs,
            _Shop().browser_session_sync() as (session, driver),
        ):
            driver.get(f"{SITE}/login")

        assert cached.call_args.args[0] is session
        assert "_gp_header_roles" not in vars(session)
        assert "login_header_capture_unavailable" in [e["event"] for e in logs]

    def test_roles_a_plugin_set_itself_are_kept(self) -> None:
        made: list[_Driver] = []
        mine = {"navigation": {"User-Agent": "pinned/1.0"}}
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            _Shop().browser_session_sync() as (session, driver),
        ):
            driver.get(f"{SITE}/login")
            session._gp_header_roles = mine

        assert cached.call_args.args[0]._gp_header_roles is mine

    def test_a_capture_that_found_no_roles_warns_once_and_the_login_caches(self) -> None:
        made: list[_Driver] = []
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            capture_logs() as logs,
            _Shop().browser_session_sync() as (session, _driver),
        ):
            pass

        empty = [e for e in logs if e["event"] == "login_header_roles_empty"]
        assert len(empty) == 1
        assert empty[0]["log_level"] == "warning"
        assert empty[0]["requests"] == 0
        assert cached.call_args.args[0] is session

    def test_a_login_that_raises_caches_nothing(self) -> None:
        made: list[_Driver] = []
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            pytest.raises(ValueError, match="login failed"),
            _Shop().browser_session_sync() as (session, driver),
        ):
            driver.get(f"{SITE}/login")
            raise ValueError("login failed")

        cached.assert_not_called()
        assert "_gp_header_roles" not in vars(session)
        made[0].quit.assert_called_once()


class TestBrowserSessionPerformanceLog:
    @pytest.mark.parametrize(
        ("performance_log", "observe_mode", "expected"),
        [
            (None, "off", False),
            (None, "full", True),
            (True, "off", True),
            (False, "off", False),
        ],
    )
    def test_the_driver_keeps_the_log_when_asked_or_observed(
        self, performance_log: bool | None, observe_mode: str, expected: bool
    ) -> None:
        from graftpunk.session import BrowserSession

        made: list[_Driver] = []
        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
        ):
            BrowserSession(
                backend="selenium",
                headless=True,
                observe_mode=observe_mode,
                performance_log=performance_log,
            )

        assert made[0].performance_log is expected


class TestGeneratedSeleniumLogin:
    def test_it_caches_header_roles_with_observe_off(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep
        from graftpunk.plugins.login_engine import generate_login_method

        class Declared(_Shop):
            site_name = "myshop"
            session_name = "myshop"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={}, submit="#go")],
                url="/login",
                success_url="/account",
            )

        plugin = Declared()
        made: list[_Driver] = []

        def outcome(**_kwargs: Any) -> bool:
            made[0].get(f"{SITE}/account")
            return True

        with (
            patch("graftpunk.stealth.create_stealth_driver", side_effect=_stealth(made)),
            _no_requestium(),
            patch(
                "graftpunk.plugins.login_engine.wait_for_login_outcome_selenium",
                side_effect=outcome,
            ),
            patch("graftpunk.plugins.login_engine.await_page_ready_selenium"),
            patch(
                "graftpunk.plugins.login_engine._find_target_selenium",
                return_value=SimpleNamespace(element=MagicMock()),
            ),
            patch(
                "graftpunk.plugins.login_engine.success_shown_before_submit_selenium",
                return_value=False,
            ),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
        ):
            assert generate_login_method(plugin)({}) is True

        assert made[0].performance_log is True
        stored = cached.call_args.args[0]
        assert stored._gp_header_roles["navigation"]["User-Agent"] == UA
        err = capsys.readouterr().err
        assert "Performance log collection failed" not in err
        assert "Browser log collection failed" not in err
        assert made[0].logs_read == ["performance"]
