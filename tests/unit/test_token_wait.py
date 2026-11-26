"""How long a browser read of a page token waits for it (``Token.wait``), which
browser a command's fallback read starts (``TokenConfig.headless``), and what a
login says about a token it could not read.

No Chrome: tabs and drivers are doubles whose pages hold a token from a given
time on, on a clock the poll's own pauses advance.
"""

from __future__ import annotations

import asyncio
import math
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests
from structlog.testing import capture_logs

from graftpunk import tokens
from graftpunk import tokens as tokens_mod
from graftpunk.exceptions import PluginError, TokenExtractionError
from graftpunk.tokens import Token, TokenConfig, _poll_for_tokens, prepare_session

PAGE = "https://myshop.example/app"


def _token(name: str = "X-CSRF", **kwargs: Any) -> Token:
    return Token(name=name, source="page", pattern=rf'{name}="([^"]+)"', page_url="/app", **kwargs)


class _Tab:
    """A nodriver tab double: its page holds each named token from the given
    second on, and its sleep() moves its clock instead of waiting."""

    def __init__(self, appears: dict[str, float]) -> None:
        self.appears = appears
        self.now = 0.0
        self.reads: list[float] = []

    async def get_content(self) -> str:
        self.reads.append(self.now)
        return "".join(f'<meta {n}="v-{n}">' for n, at in self.appears.items() if self.now >= at)

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _far() -> float:
    return asyncio.get_running_loop().time() + 600.0


# ---------------------------------------------------------------------------
# Token.wait
# ---------------------------------------------------------------------------


class TestTokenWaitField:
    def test_the_default_is_three_seconds(self) -> None:
        assert _token().wait == 3.0

    @pytest.mark.parametrize("wait", [0.1, 3, 15.5, 120])
    def test_a_positive_number_up_to_120_is_accepted(self, wait: float) -> None:
        assert _token(wait=wait).wait == wait

    @pytest.mark.parametrize("wait", [True, False, "3", None, 0, -1, 120.5, math.nan, math.inf])
    def test_anything_else_is_refused(self, wait: Any) -> None:
        with pytest.raises(ValueError, match=r"Token\.wait"):
            _token(wait=wait)

    def test_the_page_and_header_factories_take_it(self) -> None:
        assert Token.from_meta_tag("csrf", "X-CSRF", wait=10).wait == 10
        assert Token.from_hidden_input("csrf", "X-CSRF", wait=11).wait == 11
        assert Token.from_js_variable(r'c="(\w+)"', "X-CSRF", wait=12).wait == 12

    def test_the_response_header_factory_does_not_take_it(self) -> None:
        """Its token has no pattern, so no browser read ever waits for it."""
        with pytest.raises(TypeError):
            Token.from_response_header("X-Token", "X-Token", wait=13)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# The poll
# ---------------------------------------------------------------------------


class TestPoll:
    @pytest.mark.asyncio
    async def test_a_token_that_appears_at_7s_is_read_with_a_15s_wait(self) -> None:
        tab = _Tab({"X-CSRF": 7.0})
        got = await _poll_for_tokens(tab, [_token(wait=15)], PAGE, "test", end=_far())
        assert got == {"X-CSRF": "v-X-CSRF"}
        assert tab.reads[-1] == 7.0

    @pytest.mark.asyncio
    async def test_the_default_reads_for_three_seconds_and_misses_it(self) -> None:
        tab = _Tab({"X-CSRF": 7.0})
        with capture_logs() as logs:
            got = await _poll_for_tokens(tab, [_token()], PAGE, "test", end=_far())
        assert got == {}
        assert tab.reads == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        assert [e["event"] for e in logs] == ["test_pattern_not_found"]

    @pytest.mark.asyncio
    async def test_a_token_present_at_once_is_read_once(self) -> None:
        tab = _Tab({"X-CSRF": 0.0})
        assert await _poll_for_tokens(tab, [_token()], PAGE, "test", end=_far()) == {
            "X-CSRF": "v-X-CSRF"
        }
        assert tab.reads == [0.0]

    @pytest.mark.asyncio
    async def test_each_token_on_a_page_keeps_its_own_wait(self) -> None:
        tab = _Tab({"X-Late": 3.5})
        got = await _poll_for_tokens(
            tab,
            [_token("X-Never", wait=1.0), _token("X-Late", wait=4.0)],
            PAGE,
            "test",
            end=_far(),
        )
        assert got == {"X-Late": "v-X-Late"}
        assert tab.reads[-1] == 3.5

    @pytest.mark.asyncio
    async def test_a_slow_page_read_counts_against_the_wait(self) -> None:
        """A read slower than the pause: by the clock the wait has passed after
        the second read (1.2 s), while the pauses alone (0.5 s) would allow a
        third."""

        class SlowTab(_Tab):
            async def get_content(self) -> str:
                await asyncio.sleep(0.6)
                return await super().get_content()

        tab = SlowTab({})
        assert await _poll_for_tokens(tab, [_token(wait=1.0)], PAGE, "test", end=_far()) == {}
        assert len(tab.reads) == 2

    @pytest.mark.asyncio
    async def test_the_deadline_raises_with_the_tokens_already_read(self) -> None:
        class RealSleepTab(_Tab):
            async def sleep(self, seconds: float) -> None:
                await asyncio.sleep(seconds)
                await super().sleep(seconds)

        tab = RealSleepTab({"X-Early": 0.0})
        end = asyncio.get_running_loop().time() + 0.3
        with pytest.raises(TimeoutError) as raised:
            await _poll_for_tokens(
                tab, [_token("X-Early"), _token("X-Never", wait=60)], PAGE, "test", end=end
            )
        assert raised.value.results == {"X-Early": "v-X-Early"}  # type: ignore[attr-defined]


class _Clock:
    """A clock and a sleeper for the synchronous poll: reading the page takes
    *read_takes* seconds, and a pause moves the clock by *pause_moves* of what
    was asked."""

    def __init__(self, read_takes: float = 0.0, pause_moves: float = 1.0) -> None:
        self.now = 0.0
        self.read_takes = read_takes
        self.pause_moves = pause_moves
        self.reads: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds * self.pause_moves

    def page(self, appears: dict[str, float]) -> Any:
        def read() -> str:
            self.reads.append(self.now)
            html = "".join(f'<meta {n}="v-{n}">' for n, at in appears.items() if self.now >= at)
            self.now += self.read_takes
            return html

        return read


class TestSyncPoll:
    def _poll(self, clock: _Clock, read: Any, tokens: list[Token]) -> dict[str, str]:
        return tokens_mod._poll_page_tokens_sync(
            read, tokens, PAGE, "test", clock=clock.clock, sleep=clock.sleep
        )

    def test_a_token_that_appears_at_1_2s_is_read_at_1_5s(self) -> None:
        clock = _Clock()
        got = self._poll(clock, clock.page({"X-CSRF": 1.2}), [_token()])
        assert got == {"X-CSRF": "v-X-CSRF"}
        assert clock.reads == [0.0, 0.5, 1.0, 1.5]

    def test_a_token_present_at_once_is_read_once(self) -> None:
        clock = _Clock()
        assert self._poll(clock, clock.page({"X-CSRF": 0.0}), [_token()]) == {"X-CSRF": "v-X-CSRF"}
        assert clock.reads == [0.0]

    def test_a_missing_token_is_given_up_after_its_wait(self) -> None:
        clock = _Clock()
        with capture_logs() as logs:
            assert self._poll(clock, clock.page({}), [_token()]) == {}
        assert clock.reads == [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
        assert [e["event"] for e in logs] == ["test_pattern_not_found"]

    def test_a_slow_page_read_counts_against_the_wait(self) -> None:
        clock = _Clock(read_takes=0.6, pause_moves=0.0)
        assert self._poll(clock, clock.page({}), [_token(wait=1.0)]) == {}
        assert len(clock.reads) == 2

    def test_pauses_that_return_early_still_end_the_poll(self) -> None:
        clock = _Clock(pause_moves=0.0)
        assert self._poll(clock, clock.page({}), [_token()]) == {}
        assert len(clock.reads) == 7


class _NavTab(_Tab):
    """A tab whose page is the same tab after a navigation, on a real clock: the
    token appears *appears_after* real seconds after the double is made."""

    def __init__(self, appears: dict[str, float]) -> None:
        super().__init__(appears)
        self.t0 = time.monotonic()
        self.browser = SimpleNamespace(get=self._get)

    async def _get(self, _url: str) -> _NavTab:
        return self

    async def get_content(self) -> str:
        self.now = time.monotonic() - self.t0
        return await super().get_content()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


def _nav_browser(tab: _NavTab) -> Any:
    browser = MagicMock()
    browser.main_tab = tab
    browser.tabs = [tab]
    browser.get = tab._get
    browser.stop = MagicMock()
    return browser


def _browser_patches(browser: Any) -> Any:
    from contextlib import ExitStack

    stack = ExitStack()

    async def start(*, headless: bool = True) -> Any:
        return browser

    stack.enter_context(patch.object(tokens_mod, "nodriver_start", side_effect=start))
    stack.enter_context(patch.object(tokens_mod, "prepare_browser_launch", return_value=None))
    stack.enter_context(patch.object(tokens_mod, "answer_dialogs", AsyncMock()))
    stack.enter_context(patch.object(tokens_mod, "answer_dialogs_in_new_tabs", AsyncMock()))
    stack.enter_context(patch.object(tokens_mod, "close_dialogs_in_new_tabs", AsyncMock()))
    stack.enter_context(patch.object(tokens_mod, "_deregister_nodriver_browser"))
    stack.enter_context(patch.object(tokens_mod, "remove_browser_temp_profile"))
    stack.enter_context(
        patch(
            "graftpunk.session.inject_cookies_to_nodriver",
            new_callable=AsyncMock,
            return_value=(0, 0),
        )
    )
    return stack


class TestPageDeadline:
    """A page gets the navigation budget plus its tokens' longest wait, and the
    timeout event names that budget and keeps the tokens already read."""

    @pytest.mark.asyncio
    async def test_the_login_read_waits_past_the_navigation_budget(self) -> None:
        from graftpunk.tokens import extract_tokens_from_tab

        tab = _NavTab({"X-CSRF": 0.3})
        with patch.object(tokens_mod, "_TOKEN_NAV_TIMEOUT", 0.1):
            got = await extract_tokens_from_tab(tab, [_token(wait=1.0)], "https://myshop.example")
        assert got == {"X-CSRF": "v-X-CSRF"}

    @pytest.mark.asyncio
    async def test_the_fallback_read_waits_past_the_navigation_budget(self) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        tab = _NavTab({"X-CSRF": 0.3})
        with (
            _browser_patches(_nav_browser(tab)),
            patch.object(tokens_mod, "_TOKEN_NAV_TIMEOUT", 0.1),
        ):
            got = await _extract_tokens_browser(
                requests.Session(), [_token(wait=1.0)], "https://myshop.example"
            )
        assert got == {"X-CSRF": "v-X-CSRF"}

    @pytest.mark.asyncio
    async def test_a_login_read_cut_off_by_the_deadline_logs_it_and_keeps_what_it_read(
        self,
    ) -> None:
        from graftpunk.tokens import extract_tokens_from_tab

        tab = _NavTab({"X-Early": 0.0})
        # A budget of 0.3 s in all: the navigation's share is what makes it so.
        with patch.object(tokens_mod, "_TOKEN_NAV_TIMEOUT", -59.7), capture_logs() as logs:
            got = await extract_tokens_from_tab(
                tab,
                [_token("X-Early", wait=60), _token("X-Never", wait=60)],
                "https://myshop.example",
            )
        assert got == {"X-Early": "v-X-Early"}
        (event,) = [e for e in logs if e["event"] == "login_token_extraction_timeout"]
        assert event["timeout"] == pytest.approx(0.3)

    @pytest.mark.asyncio
    async def test_a_fallback_read_cut_off_by_the_deadline_logs_it_and_keeps_what_it_read(
        self,
    ) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        tab = _NavTab({"X-Early": 0.0})
        with (
            _browser_patches(_nav_browser(tab)),
            patch.object(tokens_mod, "_TOKEN_NAV_TIMEOUT", -59.7),
            capture_logs() as logs,
        ):
            got = await _extract_tokens_browser(
                requests.Session(),
                [_token("X-Early", wait=60), _token("X-Never", wait=60)],
                "https://myshop.example",
            )
        assert got == {"X-Early": "v-X-Early"}
        (event,) = [e for e in logs if e["event"] == "browser_token_navigation_timeout"]
        assert event["timeout"] == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# The fallback browser a command starts
# ---------------------------------------------------------------------------


def _http_fails(session: requests.Session) -> Any:
    return patch.object(session, "get", side_effect=requests.ConnectionError("unreachable"))


def _head_fails(session: requests.Session) -> Any:
    return patch.object(session, "head", side_effect=requests.ConnectionError("unreachable"))


class _StartedError(Exception):
    pass


def _start_recorder(started: list[bool]) -> Any:
    async def start(*, headless: bool = True) -> Any:
        started.append(headless)
        raise _StartedError

    return start


class TestFallbackBrowserMode:
    def test_the_default_starts_headless(self) -> None:
        assert TokenConfig(tokens=(_token(),)).headless is True
        session = requests.Session()
        started: list[bool] = []
        with (
            patch.object(tokens, "nodriver_start", side_effect=_start_recorder(started)),
            patch.object(tokens, "prepare_browser_launch", return_value=None),
            _http_fails(session),
            pytest.raises(_StartedError),
        ):
            prepare_session(session, TokenConfig(tokens=(_token(),)), "https://myshop.example")
        assert started == [True]

    def test_headless_false_starts_a_visible_browser(self) -> None:
        session = requests.Session()
        started: list[bool] = []
        config = TokenConfig(tokens=(_token(),), headless=False)
        with (
            patch.object(tokens, "nodriver_start", side_effect=_start_recorder(started)),
            patch.object(tokens, "prepare_browser_launch", return_value=None),
            _http_fails(session),
            pytest.raises(TokenExtractionError),
        ):
            prepare_session(session, config, "https://myshop.example")
        assert started == [False]

    def test_headless_must_be_a_bool(self) -> None:
        with pytest.raises(ValueError, match=r"TokenConfig\.headless"):
            TokenConfig(tokens=(_token(),), headless="no")  # type: ignore[arg-type]

    def test_a_visible_browser_that_cannot_start_names_the_setting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.cli.plugin_runtime import _prepare_tokens_or_exit

        async def no_display(*, headless: bool = True) -> Any:
            raise RuntimeError("Failed to connect to browser")

        session = requests.Session()
        plugin = MagicMock(site_name="myshop", base_url="https://myshop.example")
        config = TokenConfig(tokens=(_token(),), headless=False)
        with (
            patch.object(tokens, "nodriver_start", side_effect=no_display),
            patch.object(tokens, "prepare_browser_launch", return_value=None),
            _http_fails(session),
            pytest.raises(SystemExit) as exited,
        ):
            _prepare_tokens_or_exit(plugin, session, config)

        assert exited.value.code == 1
        err = " ".join(capsys.readouterr().err.split())
        assert "Token extraction failed" in err
        assert "TokenConfig(headless=False)" in err
        assert "Failed to connect to browser" in err
        assert "Command failed" not in err

    def test_a_headless_browser_that_cannot_start_fails_as_before(self) -> None:
        async def broken(*, headless: bool = True) -> Any:
            raise RuntimeError("Failed to connect to browser")

        session = requests.Session()
        with (
            patch.object(tokens, "nodriver_start", side_effect=broken),
            patch.object(tokens, "prepare_browser_launch", return_value=None),
            _http_fails(session),
            pytest.raises(RuntimeError, match="Failed to connect to browser"),
        ):
            prepare_session(session, TokenConfig(tokens=(_token(),)), "https://myshop.example")


class TestResponseHeaderTokenInTheBrowserRead:
    @pytest.mark.parametrize("extraction", ["auto", "browser"])
    def test_one_without_a_pattern_fails_naming_the_header_without_starting_a_browser(
        self, extraction: str
    ) -> None:
        session = requests.Session()
        started: list[bool] = []
        token = Token.from_response_header("X-Api-Key", "X-Api-Key", extraction=extraction)
        with (
            patch.object(tokens, "nodriver_start", side_effect=_start_recorder(started)),
            patch.object(tokens, "prepare_browser_launch", return_value=None),
            _head_fails(session),
            pytest.raises(TokenExtractionError, match="response header 'X-Api-Key'"),
        ):
            prepare_session(session, TokenConfig(tokens=(token,)), "https://myshop.example")
        assert started == []

    @pytest.mark.parametrize("extraction", ["auto", "browser"])
    def test_one_with_a_pattern_is_read_from_the_page_as_in_1_45_0(self, extraction: str) -> None:
        """1.45.0's browser read ran a response_header token's pattern on the
        page, so such a token could be read there; it still is."""
        session = requests.Session()
        token = Token(
            name="X-Api",
            source="response_header",
            response_header="X-Api",
            pattern=r'api="([^"]+)"',
            extraction=extraction,
        )
        tab = _NavTab({})

        async def page() -> str:
            return '<meta api="from-page">'

        tab.get_content = page  # type: ignore[method-assign]
        with _browser_patches(_nav_browser(tab)), _head_fails(session):
            prepare_session(session, TokenConfig(tokens=(token,)), "https://myshop.example")
        assert getattr(session, tokens_mod._CACHE_ATTR)["X-Api"].value == "from-page"


# ---------------------------------------------------------------------------
# YAML
# ---------------------------------------------------------------------------


def _yaml_plugin(tmp_path: Path, token_lines: str) -> Path:
    path = tmp_path / "myshop.yaml"
    path.write_text(
        "site_name: myshop\n"
        "base_url: https://myshop.example\n"
        "commands:\n"
        "  me:\n"
        "    help: Me\n"
        "    url: /api/me\n"
        "tokens:\n"
        "  - name: X-CSRF\n"
        "    source: page\n"
        '    pattern: \'csrf="([^"]+)"\'\n' + token_lines
    )
    return path


class TestYamlTokenFields:
    def test_wait_and_extraction_are_read(self, tmp_path: Path) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        bundle = parse_yaml_plugin(
            _yaml_plugin(tmp_path, "    wait: 15\n    extraction: browser\n")
        )
        (token,) = bundle.config.token_config.tokens
        assert token.wait == 15
        assert token.extraction == "browser"

    def test_the_defaults_hold_when_absent(self, tmp_path: Path) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        (token,) = parse_yaml_plugin(_yaml_plugin(tmp_path, "")).config.token_config.tokens
        assert (token.wait, token.extraction) == (3.0, "auto")

    @pytest.mark.parametrize("line", ["    wait: 0\n", "    wait: soon\n", "    wait: true\n"])
    def test_an_invalid_wait_names_the_file(self, tmp_path: Path, line: str) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        with pytest.raises(PluginError, match=r"myshop\.yaml.*token #1.*Token\.wait"):
            parse_yaml_plugin(_yaml_plugin(tmp_path, line))


class TestYamlExtractionAcrossReleases:
    """1.45.0 ignored a YAML token's ``extraction`` and loaded every one as
    ``auto``. From 1.46.0 the value is read, so a file that already set it runs
    differently, or fails to load."""

    @pytest.mark.parametrize("value", ["http", "browser", "auto"])
    def test_a_valid_value_is_now_the_tokens(self, tmp_path: Path, value: str) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        bundle = parse_yaml_plugin(_yaml_plugin(tmp_path, f"    extraction: {value}\n"))
        (token,) = bundle.config.token_config.tokens
        assert token.extraction == value

    @pytest.mark.parametrize("value", ["Browser", "headless"])
    def test_any_other_value_stops_the_plugin_loading(self, tmp_path: Path, value: str) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        with pytest.raises(PluginError, match=r"token #1 is invalid: Token\.extraction"):
            parse_yaml_plugin(_yaml_plugin(tmp_path, f"    extraction: {value}\n"))

    def test_http_has_no_browser_fallback(self, tmp_path: Path) -> None:
        from graftpunk.plugins.yaml_loader import parse_yaml_plugin

        config = parse_yaml_plugin(_yaml_plugin(tmp_path, "    extraction: http\n")).config
        session = requests.Session()
        started: list[bool] = []
        with (
            patch.object(tokens, "nodriver_start", side_effect=_start_recorder(started)),
            _http_fails(session),
            pytest.raises(requests.ConnectionError),
        ):
            prepare_session(session, config.token_config, config.base_url)
        assert started == []


# ---------------------------------------------------------------------------
# A login that could not read a token
# ---------------------------------------------------------------------------


class TestLoginReportsMissingTokens:
    def test_a_selenium_login_warns_and_still_caches(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.cli_plugin import LoginConfig, LoginStep, SitePlugin
        from graftpunk.plugins.login_engine import generate_login_method
        from graftpunk.tokens import _CACHE_ATTR

        class Shop(SitePlugin):
            site_name = "myshop"
            session_name = "myshop"
            help_text = "Shop"
            base_url = "https://myshop.example"
            backend = "selenium"
            login_config = LoginConfig(
                steps=[LoginStep(fields={"username": "#user"}, submit="#submit")],
                url="/login",
                success=".dashboard",
                timeout=0.05,
                settle=0.0,
            )
            token_config = TokenConfig(
                tokens=(
                    Token.from_meta_tag("csrf", "X-CSRF"),
                    Token.from_meta_tag("late", "X-Late", page_url="/app"),
                    Token.from_cookie("xsrf", "X-XSRF"),
                    Token.from_response_header("X-Api", "X-Api"),
                )
            )

        bs = MagicMock()
        instance = bs.return_value
        instance.__enter__ = MagicMock(return_value=instance)
        instance.__exit__ = MagicMock(return_value=False)
        instance.driver.page_source = '<div class="dashboard"><meta name="csrf" content="c1">'
        instance.cookies = requests.cookies.RequestsCookieJar()
        capture = MagicMock()
        capture.get_header_roles.return_value = {}
        capture.get_header_role_origins.return_value = {}

        with (
            patch("graftpunk.BrowserSession", bs),
            patch("graftpunk.plugins.cli_plugin.cache_session") as cached,
            patch("graftpunk.plugins.login_engine.time.sleep"),
            # The token poll's own pauses, so no real second passes.
            patch("graftpunk.tokens.time.sleep"),
            patch("graftpunk.observe.capture.create_capture_backend", return_value=capture),
            capture_logs() as logs,
        ):
            assert generate_login_method(Shop())({"username": "u"}) is True

        cached.assert_called_once()
        assert set(getattr(instance, _CACHE_ATTR)) == {"X-CSRF"}
        (missing,) = [e for e in logs if e["event"] == "login_tokens_missing"]
        assert missing["tokens"] == ["X-Late", "X-XSRF"]
        err = " ".join(capsys.readouterr().err.split())
        assert "X-Late" in err
        assert "X-XSRF" in err
        assert "X-Api" not in err
        assert "Token.wait" in err
        # It prints before the session is saved, so it must not say it was.
        assert "saved" not in err

    @pytest.mark.asyncio
    async def test_a_nodriver_login_warns_naming_the_missing_token(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.login_engine import _extract_and_cache_tokens_nodriver
        from graftpunk.tokens import _CACHE_ATTR

        tab = _Tab({"X-CSRF": 0.0})
        tab.browser = MagicMock()

        async def get(_url: str) -> _Tab:
            return tab

        tab.browser.get = get
        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(_token(), _token("X-Gone")))
        session = MagicMock()

        with capture_logs() as logs:
            await _extract_and_cache_tokens_nodriver(plugin, session, tab, "https://myshop.example")

        assert set(getattr(session, _CACHE_ATTR)) == {"X-CSRF"}
        assert [e["tokens"] for e in logs if e["event"] == "login_tokens_missing"] == [["X-Gone"]]
        assert "X-Gone" in " ".join(capsys.readouterr().err.split())

    def test_a_login_that_read_every_token_says_nothing(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.login_engine import _extract_and_cache_tokens_selenium

        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(_token(),))
        session = MagicMock()
        session.driver.page_source = '<meta X-CSRF="v">'
        with capture_logs() as logs:
            _extract_and_cache_tokens_selenium(plugin, session, "https://myshop.example")
        assert not [e for e in logs if e["event"] == "login_tokens_missing"]
        assert capsys.readouterr().err == ""


def _two_domain_jar(values: tuple[str, str]) -> requests.cookies.RequestsCookieJar:
    jar = requests.cookies.RequestsCookieJar()
    jar.set("xsrf", values[0], domain="myshop.example")
    jar.set("xsrf", values[1], domain="api.myshop.example")
    return jar


class TestLoginReportsTokensItCouldNotRead:
    @pytest.mark.asyncio
    async def test_a_nodriver_page_read_that_raises_still_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.login_engine import _extract_and_cache_tokens_nodriver

        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(_token(), Token.from_cookie("sid", "X-Sid")))
        session = MagicMock()
        session.cookies = requests.cookies.RequestsCookieJar()
        session.cookies.set("sid", "s1", domain="myshop.example")
        with (
            patch(
                "graftpunk.tokens.extract_tokens_from_tab",
                AsyncMock(side_effect=RuntimeError("tab closed")),
            ),
            capture_logs() as logs,
        ):
            await _extract_and_cache_tokens_nodriver(plugin, session, MagicMock(), PAGE)

        assert set(getattr(session, tokens._CACHE_ATTR)) == {"X-Sid"}
        (missing,) = [e for e in logs if e["event"] == "login_tokens_missing"]
        assert missing["tokens"] == ["X-CSRF"]
        err = " ".join(capsys.readouterr().err.split())
        assert "X-CSRF" in err
        assert "RuntimeError" in err

    def test_a_selenium_page_read_that_raises_still_warns(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins.login_engine import _extract_and_cache_tokens_selenium

        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(_token(),))
        session = MagicMock()
        session.driver.get.side_effect = RuntimeError("chromedriver went away")
        with capture_logs() as logs:
            _extract_and_cache_tokens_selenium(plugin, session, "https://myshop.example")

        assert [e["tokens"] for e in logs if e["event"] == "login_tokens_missing"] == [["X-CSRF"]]
        err = " ".join(capsys.readouterr().err.split())
        assert "X-CSRF" in err
        assert "RuntimeError" in err

    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    @pytest.mark.asyncio
    async def test_a_cookie_on_two_domains_with_two_values_is_reported(
        self, backend: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from graftpunk.plugins import login_engine

        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(Token.from_cookie("xsrf", "X-XSRF"),))
        session = MagicMock()
        session.cookies = _two_domain_jar(("a", "b"))
        with capture_logs() as logs:
            if backend == "nodriver":
                await login_engine._extract_and_cache_tokens_nodriver(
                    plugin, session, MagicMock(), PAGE
                )
            else:
                login_engine._extract_and_cache_tokens_selenium(plugin, session, PAGE)

        assert [e["tokens"] for e in logs if e["event"] == "login_tokens_missing"] == [["X-XSRF"]]
        err = " ".join(capsys.readouterr().err.split())
        assert "X-XSRF" in err
        assert "2 domains" in err

    @pytest.mark.parametrize("backend", ["nodriver", "selenium"])
    @pytest.mark.asyncio
    async def test_a_cookie_on_two_domains_with_one_value_is_found(self, backend: str) -> None:
        from graftpunk.plugins import login_engine

        plugin = MagicMock(site_name="myshop")
        plugin.token_config = TokenConfig(tokens=(Token.from_cookie("xsrf", "X-XSRF"),))
        session = MagicMock()
        session.cookies = _two_domain_jar(("same", "same"))
        with capture_logs() as logs:
            if backend == "nodriver":
                await login_engine._extract_and_cache_tokens_nodriver(
                    plugin, session, MagicMock(), PAGE
                )
            else:
                login_engine._extract_and_cache_tokens_selenium(plugin, session, PAGE)

        assert getattr(session, tokens._CACHE_ATTR)["X-XSRF"].value == "same"
        assert not [e for e in logs if e["event"] == "login_tokens_missing"]
