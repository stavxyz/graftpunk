"""Tests for browser-based token extraction."""

from __future__ import annotations

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
