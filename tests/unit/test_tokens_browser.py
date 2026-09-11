"""Tests for browser-based token extraction."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from graftpunk.tokens import (
    Token,
    TokenConfig,
    _poll_for_tokens,
    prepare_session,
)


class _AwaitableMock(MagicMock):
    """MagicMock subclass that simulates nodriver Tab (awaitable + async sleep)."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.sleep = AsyncMock()  # nodriver tab.sleep() is async

    def __await__(self):  # type: ignore[override]
        return iter([])


class TestExtractTokensBrowser:
    """Tests for _extract_tokens_browser async function."""

    @pytest.mark.asyncio
    async def test_extracts_single_token_from_page(self) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        session.cookies.set("sid", "abc", domain="example.com")
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/dashboard",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='<html>var csrf = "tok123";</html>')

        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = mock_tab

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(1, 0),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
        ):
            results = await _extract_tokens_browser(session, [token], "https://example.com")

        assert results == {"X-CSRF": "tok123"}

    @pytest.mark.asyncio
    async def test_groups_tokens_by_url(self) -> None:
        """Two tokens from same page_url should share one navigation."""
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        token1 = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )
        token2 = Token(
            name="X-Nonce",
            source="page",
            pattern=r'nonce = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='csrf = "aaa"; nonce = "bbb";')
        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = mock_tab

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(1, 0),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
        ):
            results = await _extract_tokens_browser(
                session, [token1, token2], "https://example.com"
            )

        assert results == {"X-CSRF": "aaa", "X-Nonce": "bbb"}
        mock_browser.get.assert_called_once()

    @pytest.mark.asyncio
    async def test_partial_failure_url_a_succeeds_url_b_fails(self) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        token_a = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/good",
            extraction="browser",
        )
        token_b = Token(
            name="X-Nonce",
            source="page",
            pattern=r'nonce = "([^"]+)"',
            page_url="/bad",
            extraction="browser",
        )

        mock_tab_good = _AwaitableMock()
        mock_tab_good.get_content = AsyncMock(return_value='csrf = "val1";')

        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(side_effect=[mock_tab_good, RuntimeError("Navigation failed")])
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = _AwaitableMock()

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(1, 0),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
        ):
            results = await _extract_tokens_browser(
                session, [token_a, token_b], "https://example.com"
            )

        assert results == {"X-CSRF": "val1"}
        assert "X-Nonce" not in results

    @pytest.mark.asyncio
    async def test_timeout_skips_url_and_continues(self) -> None:
        """Per-URL timeout skips the timed-out URL, extracts remaining."""
        import asyncio

        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        token_slow = Token(
            name="X-Slow",
            source="page",
            pattern=r'slow = "([^"]+)"',
            page_url="/slow",
            extraction="browser",
        )
        token_fast = Token(
            name="X-Fast",
            source="page",
            pattern=r'fast = "([^"]+)"',
            page_url="/fast",
            extraction="browser",
        )

        mock_tab_fast = _AwaitableMock()
        mock_tab_fast.get_content = AsyncMock(return_value='fast = "val1";')

        async def _side_effect(url):
            if "/slow" in url:
                await asyncio.sleep(999)
            return mock_tab_fast

        mock_browser = AsyncMock()
        mock_browser.get = _side_effect
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = _AwaitableMock()

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(0, 0),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
            patch("graftpunk.tokens._TOKEN_NAV_TIMEOUT", 0.05),
        ):
            results = await _extract_tokens_browser(
                session, [token_slow, token_fast], "https://example.com"
            )

        assert "X-Slow" not in results
        assert results == {"X-Fast": "val1"}

    @pytest.mark.asyncio
    async def test_pattern_not_found_excluded_from_results(self) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/page",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value="<html>no token here</html>")
        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = mock_tab

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=(1, 0),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
        ):
            results = await _extract_tokens_browser(session, [token], "https://example.com")

        assert results == {}

    @pytest.mark.asyncio
    async def test_injects_session_cookies(self) -> None:
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        session.cookies.set("sid", "abc", domain="example.com")
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='csrf = "v1";')
        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = mock_tab

        mock_inject = AsyncMock(return_value=(1, 0))

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch("graftpunk.session.inject_cookies_to_nodriver", mock_inject),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
        ):
            await _extract_tokens_browser(session, [token], "https://example.com")

        mock_inject.assert_called_once_with(mock_tab, session.cookies)

    @pytest.mark.asyncio
    async def test_browser_always_stopped_on_error(self) -> None:
        """Browser must be stopped even if cookie injection fails."""
        from graftpunk.tokens import _extract_tokens_browser

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/",
            extraction="browser",
        )

        mock_browser = AsyncMock()
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = _AwaitableMock()

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                side_effect=RuntimeError("CDP fail"),
            ),
            patch("graftpunk.tokens._deregister_nodriver_browser"),
            pytest.raises(RuntimeError, match="CDP fail"),
        ):
            await _extract_tokens_browser(session, [token], "https://example.com")

        mock_browser.stop.assert_called_once()


class TestPrepareSessionBrowserFallback:
    """Tests for prepare_session with browser extraction fallback."""

    def test_auto_token_falls_back_to_browser(self) -> None:
        """extraction='auto' token that fails HTTP gets extracted via browser."""
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="auto",
        )
        config = TokenConfig(tokens=(token,))

        with (
            patch.object(session, "get", side_effect=requests.exceptions.ReadTimeout("timeout")),
            patch(
                "graftpunk.tokens._run_browser_extraction",
                return_value={"X-CSRF": "browser_val"},
            ) as mock_browser,
        ):
            prepare_session(session, config, "https://example.com")

        csrf_tokens = getattr(session, "_gp_csrf_tokens", {})
        assert csrf_tokens["X-CSRF"] == "browser_val"
        mock_browser.assert_called_once()

    def test_browser_token_skips_http(self) -> None:
        """extraction='browser' token goes directly to browser batch."""
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )
        config = TokenConfig(tokens=(token,))

        with patch(
            "graftpunk.tokens._run_browser_extraction",
            return_value={"X-CSRF": "browser_val"},
        ) as mock_browser:
            prepare_session(session, config, "https://example.com")

        csrf_tokens = getattr(session, "_gp_csrf_tokens", {})
        assert csrf_tokens["X-CSRF"] == "browser_val"
        mock_browser.assert_called_once()

    def test_mixed_tokens_cookie_and_browser(self) -> None:
        """Cookie token resolves immediately; browser token batched."""
        session = requests.Session()
        session.cookies.set("sid", "cookie_val")
        cookie_token = Token(name="X-Session", source="cookie", cookie_name="sid")
        browser_token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )
        config = TokenConfig(tokens=(cookie_token, browser_token))

        with patch(
            "graftpunk.tokens._run_browser_extraction",
            return_value={"X-CSRF": "browser_val"},
        ):
            prepare_session(session, config, "https://example.com")

        csrf_tokens = getattr(session, "_gp_csrf_tokens", {})
        assert csrf_tokens["X-Session"] == "cookie_val"
        assert csrf_tokens["X-CSRF"] == "browser_val"

    def test_browser_extraction_missing_token_raises(self) -> None:
        """If browser extraction returns empty dict, raise ValueError."""
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )
        config = TokenConfig(tokens=(token,))

        with (
            patch("graftpunk.tokens._run_browser_extraction", return_value={}),
            pytest.raises(ValueError, match="Browser extraction failed for token 'X-CSRF'"),
        ):
            prepare_session(session, config, "https://example.com")

    def test_no_browser_tokens_skips_browser(self) -> None:
        """All-cookie config never calls browser extraction."""
        session = requests.Session()
        session.cookies.set("csrf", "val")
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrf")
        config = TokenConfig(tokens=(token,))

        with patch("graftpunk.tokens._run_browser_extraction") as mock_browser:
            prepare_session(session, config, "https://example.com")

        mock_browser.assert_not_called()
        csrf_tokens = getattr(session, "_gp_csrf_tokens", {})
        assert csrf_tokens["X-CSRF"] == "val"

    def test_browser_tokens_are_cached(self) -> None:
        """Browser-extracted tokens are cached in the session."""
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
            cache_duration=300,
        )
        config = TokenConfig(tokens=(token,))

        with patch(
            "graftpunk.tokens._run_browser_extraction",
            return_value={"X-CSRF": "browser_val"},
        ):
            prepare_session(session, config, "https://example.com")

        # Second call should use cache, not browser
        with patch("graftpunk.tokens._run_browser_extraction") as mock_browser:
            prepare_session(session, config, "https://example.com")
            mock_browser.assert_not_called()

        csrf_tokens = getattr(session, "_gp_csrf_tokens", {})
        assert csrf_tokens["X-CSRF"] == "browser_val"


class TestExtractTokensFromTab:
    """Tests for extract_tokens_from_tab async function."""

    @pytest.mark.asyncio
    async def test_extracts_token_from_existing_tab(self) -> None:
        """Extracts tokens using an already-open browser tab."""
        from graftpunk.tokens import Token, extract_tokens_from_tab

        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/dashboard",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='<html>var csrf = "tok123";</html>')
        mock_browser = MagicMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_tab.browser = mock_browser

        results = await extract_tokens_from_tab(mock_tab, [token], "https://example.com")
        assert results == {"X-CSRF": "tok123"}

    @pytest.mark.asyncio
    async def test_no_browser_started(self) -> None:
        """extract_tokens_from_tab does NOT start a new browser."""
        from graftpunk.tokens import Token, extract_tokens_from_tab

        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/",
            extraction="browser",
        )

        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='csrf = "v1";')
        mock_browser = MagicMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_tab.browser = mock_browser

        with patch("graftpunk.tokens.nodriver_start") as mock_start:
            await extract_tokens_from_tab(mock_tab, [token], "https://example.com")

        mock_start.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_non_page_tokens(self) -> None:
        """Cookie-source tokens are skipped (not page-extractable)."""
        from graftpunk.tokens import Token, extract_tokens_from_tab

        token = Token(name="X-Session", source="cookie", cookie_name="sid")

        mock_tab = _AwaitableMock()
        mock_tab.browser = MagicMock()

        results = await extract_tokens_from_tab(mock_tab, [token], "https://example.com")
        assert results == {}

    @pytest.mark.asyncio
    async def test_timeout_skips_url_and_continues(self) -> None:
        """Per-URL timeout skips the timed-out URL without crashing."""
        import asyncio

        from graftpunk.tokens import Token, extract_tokens_from_tab

        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/slow",
            extraction="browser",
        )

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(999)

        mock_tab = _AwaitableMock()
        mock_browser = MagicMock()
        mock_browser.get = _hang
        mock_tab.browser = mock_browser

        with patch("graftpunk.tokens._TOKEN_NAV_TIMEOUT", 0.05):
            results = await extract_tokens_from_tab(mock_tab, [token], "https://example.com")

        assert results == {}


class TestPollForTokens:
    """Direct tests for _poll_for_tokens retry logic."""

    @pytest.mark.asyncio
    async def test_found_on_first_attempt_no_sleep(self) -> None:
        """Token present immediately — no retry sleep needed."""
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf="([^"]+)"',
            page_url="/dash",
        )
        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value='<meta csrf="abc123">')

        results = await _poll_for_tokens(mock_tab, [token], "https://x.com/dash", "test")

        assert results == {"X-CSRF": "abc123"}
        mock_tab.sleep.assert_not_called()

    @pytest.mark.asyncio
    async def test_found_on_retry(self) -> None:
        """Token not found on first attempt but found on second."""
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf="([^"]+)"',
            page_url="/dash",
        )
        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(
            side_effect=["<html>loading...</html>", '<meta csrf="abc123">']
        )

        results = await _poll_for_tokens(mock_tab, [token], "https://x.com/dash", "test")

        assert results == {"X-CSRF": "abc123"}
        mock_tab.sleep.assert_called_once()

    @pytest.mark.asyncio
    async def test_all_attempts_exhausted(self) -> None:
        """All polling attempts fail — returns empty and logs warning."""
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf="([^"]+)"',
            page_url="/dash",
        )
        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(return_value="<html>no token here</html>")

        results = await _poll_for_tokens(mock_tab, [token], "https://x.com/dash", "test")

        assert results == {}

    @pytest.mark.asyncio
    async def test_multiple_tokens_partial_match(self) -> None:
        """One token found on first attempt, second found on retry."""
        t1 = Token(name="X-A", source="page", pattern=r'a="([^"]+)"', page_url="/p")
        t2 = Token(name="X-B", source="page", pattern=r'b="([^"]+)"', page_url="/p")
        mock_tab = _AwaitableMock()
        mock_tab.get_content = AsyncMock(side_effect=['<meta a="1">', '<meta a="1" b="2">'])

        results = await _poll_for_tokens(mock_tab, [t1, t2], "https://x.com/p", "test")

        assert results == {"X-A": "1", "X-B": "2"}


def _hygiene_browser(profile: Path | None = None) -> MagicMock:
    """A fake nodriver browser whose config says nodriver made its profile directory."""
    tab = _AwaitableMock()
    tab.get_content = AsyncMock(return_value='csrf = "v1";')
    browser = MagicMock()
    browser.main_tab = tab
    browser.get = AsyncMock(return_value=tab)
    browser.stop = MagicMock()
    browser.config.uses_custom_data_dir = False
    browser.config.user_data_dir = str(profile) if profile is not None else None
    return browser


_HYGIENE_TOKEN = Token(
    name="X-CSRF",
    source="page",
    pattern=r'csrf = "([^"]+)"',
    page_url="/app",
    extraction="browser",
)


@contextmanager
def _patched_nodriver(start: object) -> Iterator[None]:
    """Everything outside the code under test: the nodriver module and the registry."""
    module = MagicMock()
    module.start = start
    with (
        patch.dict("sys.modules", {"nodriver": module}),
        patch(
            "graftpunk.session.inject_cookies_to_nodriver",
            new_callable=AsyncMock,
            return_value=(0, 0),
        ),
        patch("graftpunk.tokens._deregister_nodriver_browser"),
    ):
        yield


class TestBrowserExtractionHygiene:
    """The third launch site marks, sweeps, registers and cleans up like the other two (#96)."""

    @pytest.mark.asyncio
    async def test_the_shared_switches_reach_nodriver_start(self) -> None:
        from graftpunk.chrome_orphans import base_browser_args
        from graftpunk.tokens import _extract_tokens_browser

        started: list[dict] = []

        async def fake_start(**kwargs: object) -> MagicMock:
            started.append(kwargs)
            return _hygiene_browser()

        with _patched_nodriver(fake_start):
            await _extract_tokens_browser(
                requests.Session(), [_HYGIENE_TOKEN], "https://example.com"
            )

        (kwargs,) = started
        assert kwargs["browser_args"] == base_browser_args()

    @pytest.mark.asyncio
    async def test_the_sweep_runs_before_the_browser_starts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from graftpunk.browser_launch import CleanupReport
        from graftpunk.tokens import _extract_tokens_browser

        order: list[str] = []

        def fake_prepare() -> CleanupReport:
            order.append("sweep")
            return CleanupReport()

        monkeypatch.setattr("graftpunk.tokens.prepare_browser_launch", fake_prepare)

        async def fake_start(**kwargs: object) -> MagicMock:
            order.append("start")
            return _hygiene_browser()

        with _patched_nodriver(fake_start):
            await _extract_tokens_browser(
                requests.Session(), [_HYGIENE_TOKEN], "https://example.com"
            )

        assert order == ["sweep", "start"]

    @pytest.mark.asyncio
    async def test_the_browser_is_registered_for_signals_and_released_on_stop(self) -> None:
        from graftpunk.signals import live_browsers
        from graftpunk.tokens import _extract_tokens_browser

        browser = _hygiene_browser()
        registered: list = []

        async def get(_url: str) -> object:
            registered.extend(live_browsers())
            return browser.main_tab

        browser.get = get

        with _patched_nodriver(AsyncMock(return_value=browser)):
            await _extract_tokens_browser(
                requests.Session(), [_HYGIENE_TOKEN], "https://example.com"
            )

        assert any(getattr(handle, "_browser", None) is browser for handle in registered)
        assert not any(getattr(handle, "_browser", None) is browser for handle in live_browsers())

    @pytest.mark.asyncio
    async def test_the_temp_profile_is_removed_after_the_browser_stops(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The leak this site had: nodriver made the directory and nobody deleted it."""
        from graftpunk.tokens import _extract_tokens_browser

        profile = _temp_profile(tmp_path, monkeypatch, "uc_tokens")

        with _patched_nodriver(AsyncMock(return_value=_hygiene_browser(profile))):
            await _extract_tokens_browser(
                requests.Session(), [_HYGIENE_TOKEN], "https://example.com"
            )

        assert not profile.exists()

    @pytest.mark.asyncio
    async def test_a_stop_that_raises_still_releases_the_browser(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The releases sit in a finally behind the stop, so a broken stop leaks nothing."""
        from graftpunk.signals import live_browsers
        from graftpunk.tokens import _extract_tokens_browser

        profile = _temp_profile(tmp_path, monkeypatch, "uc_raised")
        browser = _hygiene_browser(profile)
        browser.stop = MagicMock(side_effect=ValueError("stop blew up"))

        with (
            _patched_nodriver(AsyncMock(return_value=browser)),
            pytest.raises(ValueError, match="stop blew up"),
        ):
            await _extract_tokens_browser(
                requests.Session(), [_HYGIENE_TOKEN], "https://example.com"
            )

        assert not profile.exists()
        assert not any(getattr(handle, "_browser", None) is browser for handle in live_browsers())


def _temp_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    """A uc_ directory under a temp root this test owns, so no real profile is at risk."""
    import tempfile

    temp_root = tmp_path / "tmp"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_root))
    profile = temp_root / name
    (profile / "Default").mkdir(parents=True)
    return profile


class TestBrowserExtractionArmsTheHandlers:
    """Arming runs on the thread that calls in, not in the worker the extraction may use."""

    def test_the_sync_path_arms_them_on_the_calling_thread(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``signal.signal`` raises off the main thread and the installer swallows that.

        The async branch hands the coroutine to a worker thread, so arming
        inside the extraction would leave the process with no handlers and no
        error.
        """
        import signal as signal_module

        from graftpunk import chrome_orphans, signals
        from graftpunk.browser_launch import CleanupReport
        from graftpunk.tokens import _run_browser_extraction

        monkeypatch.setattr(chrome_orphans, "_ARMED", True)
        # The sweep is replaced at this module's call site, so an armed pass in
        # this test can never reach the real process table.
        monkeypatch.setattr("graftpunk.tokens.prepare_browser_launch", CleanupReport)
        monkeypatch.setattr(signals, "auto_install", True)
        signal_module.signal(signal_module.SIGTERM, signal_module.SIG_DFL)

        with patch(
            "graftpunk.tokens._extract_tokens_browser", new_callable=AsyncMock
        ) as mock_extract:
            mock_extract.return_value = {}
            _run_browser_extraction(requests.Session(), [_HYGIENE_TOKEN], "https://example.com")

        assert signal_module.getsignal(signal_module.SIGTERM) is signals._handle_termination
