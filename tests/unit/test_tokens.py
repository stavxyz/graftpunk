"""Tests for CSRF token and dynamic header extraction."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from graftpunk.tokens import (
    CachedToken,
    Token,
    TokenConfig,
    clear_cached_tokens,
    extract_token,
    prepare_session,
)


class TestToken:
    """Tests for Token dataclass validation and factory methods."""

    def test_token_validation_empty_name(self) -> None:
        with pytest.raises(ValueError, match="Token.name must be non-empty"):
            Token(name="", source="cookie", cookie_name="csrf")

    def test_token_validation_invalid_source(self) -> None:
        with pytest.raises(ValueError, match="Token.source must be"):
            Token(name="X-CSRF", source="invalid")

    def test_token_page_requires_pattern(self) -> None:
        with pytest.raises(ValueError, match="requires a pattern"):
            Token(name="X-CSRF", source="page")

    def test_token_cookie_requires_cookie_name(self) -> None:
        with pytest.raises(ValueError, match="requires cookie_name"):
            Token(name="X-CSRF", source="cookie")

    def test_token_response_header_requires_header(self) -> None:
        with pytest.raises(ValueError, match="requires response_header"):
            Token(name="X-CSRF", source="response_header")

    def test_from_meta_tag(self) -> None:
        token = Token.from_meta_tag("csrf-token", "X-CSRF-Token")
        assert token.name == "X-CSRF-Token"
        assert token.source == "page"
        assert token.pattern is not None
        assert "csrf\\-token" in token.pattern
        assert token.page_url == "/"
        assert token.cache_duration == 300

    def test_from_cookie(self) -> None:
        token = Token.from_cookie("csrftoken", "X-CSRFToken")
        assert token.name == "X-CSRFToken"
        assert token.source == "cookie"
        assert token.cookie_name == "csrftoken"
        assert token.cache_duration == 300

    def test_from_js_variable(self) -> None:
        token = Token.from_js_variable(r'var csrf = "([^"]+)"', "X-CSRF", page_url="/app")
        assert token.name == "X-CSRF"
        assert token.source == "page"
        assert token.pattern == r'var csrf = "([^"]+)"'
        assert token.page_url == "/app"

    def test_from_response_header(self) -> None:
        token = Token.from_response_header("X-CSRF-Token", "X-Request-CSRF")
        assert token.name == "X-Request-CSRF"
        assert token.source == "response_header"
        assert token.response_header == "X-CSRF-Token"

    def test_valid_page_token(self) -> None:
        token = Token(name="X-CSRF", source="page", pattern=r"csrf=(\w+)")
        assert token.name == "X-CSRF"
        assert token.source == "page"

    def test_valid_cookie_token(self) -> None:
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        assert token.name == "X-CSRF"
        assert token.cookie_name == "csrftoken"

    def test_extraction_defaults_to_auto(self) -> None:
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrf")
        assert token.extraction == "auto"

    def test_extraction_accepts_http(self) -> None:
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrf", extraction="http")
        assert token.extraction == "http"

    def test_extraction_accepts_browser(self) -> None:
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrf", extraction="browser")
        assert token.extraction == "browser"

    def test_extraction_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError, match="Token.extraction must be"):
            Token(name="X-CSRF", source="cookie", cookie_name="csrf", extraction="invalid")

    def test_from_js_variable_with_browser_extraction(self) -> None:
        token = Token.from_js_variable(r'csrf = "([^"]+)"', "X-CSRF", extraction="browser")
        assert token.extraction == "browser"

    def test_from_meta_tag_with_extraction(self) -> None:
        token = Token.from_meta_tag("csrf-token", "X-CSRF", extraction="browser")
        assert token.extraction == "browser"

    def test_from_response_header_with_extraction(self) -> None:
        token = Token.from_response_header("X-Token", "X-Req", extraction="http")
        assert token.extraction == "http"


class TestTokenConfig:
    """Tests for TokenConfig validation."""

    def test_empty_tokens_raises(self) -> None:
        with pytest.raises(ValueError, match="TokenConfig.tokens must be non-empty"):
            TokenConfig(tokens=())

    def test_valid_config(self) -> None:
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrf")
        config = TokenConfig(tokens=(token,))
        assert len(config.tokens) == 1
        assert config.tokens[0].name == "X-CSRF"


class TestCachedToken:
    """Tests for CachedToken TTL logic."""

    def test_not_expired(self) -> None:
        cached = CachedToken(name="X-CSRF", value="abc123", extracted_at=time.time(), ttl=300)
        assert not cached.is_expired

    def test_expired(self) -> None:
        cached = CachedToken(name="X-CSRF", value="abc123", extracted_at=time.time() - 400, ttl=300)
        assert cached.is_expired


class TestExtractToken:
    """Tests for extract_token function."""

    def test_extract_from_cookie(self) -> None:
        session = requests.Session()
        session.cookies.set("csrftoken", "secret123")
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        value = extract_token(session, token, "https://example.com")
        assert value == "secret123"

    def test_extract_from_cookie_missing(self) -> None:
        session = requests.Session()
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        with pytest.raises(ValueError, match="Cookie 'csrftoken' not found"):
            extract_token(session, token, "https://example.com")

    def test_extract_from_page(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf_token = "([^"]+)"',
            page_url="/dashboard",
        )
        mock_resp = MagicMock()
        mock_resp.text = 'var csrf_token = "abc123xyz";'
        mock_resp.raise_for_status = MagicMock()

        with patch.object(session, "get", return_value=mock_resp) as mock_get:
            value = extract_token(session, token, "https://example.com")
            assert value == "abc123xyz"
            mock_get.assert_called_once_with("https://example.com/dashboard", timeout=15)

    def test_extract_from_page_pattern_not_found(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf_token = "([^"]+)"',
            page_url="/dashboard",
            extraction="http",
        )
        mock_resp = MagicMock()
        mock_resp.text = "<html>no token here</html>"
        mock_resp.raise_for_status = MagicMock()

        with (
            patch.object(session, "get", return_value=mock_resp),
            pytest.raises(ValueError, match="Token pattern not found"),
        ):
            extract_token(session, token, "https://example.com")

    def test_extract_from_response_header(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="response_header",
            response_header="X-CSRF-Token",
            page_url="/api",
        )
        mock_resp = MagicMock()
        mock_resp.headers = {"X-CSRF-Token": "header_value_123"}

        with patch.object(session, "head", return_value=mock_resp) as mock_head:
            value = extract_token(session, token, "https://example.com")
            assert value == "header_value_123"
            mock_head.assert_called_once_with(
                "https://example.com/api", timeout=10, allow_redirects=True
            )

    def test_extract_from_response_header_missing(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="response_header",
            response_header="X-CSRF-Token",
            page_url="/api",
        )
        mock_resp = MagicMock()
        mock_resp.headers = {}

        with (
            patch.object(session, "head", return_value=mock_resp),
            pytest.raises(ValueError, match="Header 'X-CSRF-Token' not found"),
        ):
            extract_token(session, token, "https://example.com")

    def test_extract_unknown_source(self) -> None:
        """Test that an unknown source raises ValueError.

        We bypass __post_init__ validation using object.__setattr__ on a frozen
        dataclass to test the extract_token function's own guard clause.
        """
        token = Token(name="X-CSRF", source="cookie", cookie_name="x")
        # Force an invalid source past validation
        object.__setattr__(token, "source", "unknown")
        session = requests.Session()
        with pytest.raises(ValueError, match="Unknown token source"):
            extract_token(session, token, "https://example.com")


class TestPrepareSession:
    """Tests for prepare_session function."""

    def test_extracts_and_injects_token(self) -> None:
        session = requests.Session()
        session.cookies.set("csrftoken", "injected_value")
        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        config = TokenConfig(tokens=(token,))

        result = prepare_session(session, config, "https://example.com")
        assert result is session
        assert session.headers["X-CSRF"] == "injected_value"

    def test_uses_cached_token(self) -> None:
        session = requests.Session()
        # Pre-populate cache
        cached = CachedToken(name="X-CSRF", value="cached_value", extracted_at=time.time(), ttl=300)
        session._gp_cached_tokens = {"X-CSRF": cached}  # type: ignore[attr-defined]

        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        config = TokenConfig(tokens=(token,))

        # Should use cache, not try to extract (no cookie set, would fail otherwise)
        prepare_session(session, config, "https://example.com")
        assert session.headers["X-CSRF"] == "cached_value"

    def test_re_extracts_expired_token(self) -> None:
        session = requests.Session()
        session.cookies.set("csrftoken", "fresh_value")

        # Pre-populate cache with expired token
        cached = CachedToken(
            name="X-CSRF",
            value="old_value",
            extracted_at=time.time() - 400,
            ttl=300,
        )
        session._gp_cached_tokens = {"X-CSRF": cached}  # type: ignore[attr-defined]

        token = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        config = TokenConfig(tokens=(token,))

        prepare_session(session, config, "https://example.com")
        assert session.headers["X-CSRF"] == "fresh_value"

    def test_multiple_tokens(self) -> None:
        session = requests.Session()
        session.cookies.set("csrftoken", "csrf_val")
        session.cookies.set("session_id", "sess_val")

        token1 = Token(name="X-CSRF", source="cookie", cookie_name="csrftoken")
        token2 = Token(name="X-Session", source="cookie", cookie_name="session_id")
        config = TokenConfig(tokens=(token1, token2))

        prepare_session(session, config, "https://example.com")
        assert session.headers["X-CSRF"] == "csrf_val"
        assert session.headers["X-Session"] == "sess_val"


class TestClearCachedTokens:
    """Tests for clear_cached_tokens function."""

    def test_clears_cache(self) -> None:
        session = requests.Session()
        cached = CachedToken(name="X-CSRF", value="val", extracted_at=time.time(), ttl=300)
        session._gp_cached_tokens = {"X-CSRF": cached}  # type: ignore[attr-defined]

        clear_cached_tokens(session)
        assert session._gp_cached_tokens == {}  # type: ignore[attr-defined]

    def test_no_cache_no_error(self) -> None:
        session = requests.Session()
        # Should not raise
        clear_cached_tokens(session)


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

        mock_tab = AsyncMock()
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
                return_value=1,
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

        mock_tab = AsyncMock()
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
                return_value=1,
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

        mock_tab_good = AsyncMock()
        mock_tab_good.get_content = AsyncMock(return_value='csrf = "val1";')

        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(side_effect=[mock_tab_good, RuntimeError("Navigation failed")])
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = AsyncMock()

        with (
            patch("graftpunk.tokens.nodriver_start", return_value=mock_browser),
            patch(
                "graftpunk.session.inject_cookies_to_nodriver",
                new_callable=AsyncMock,
                return_value=1,
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

        mock_tab = AsyncMock()
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
                return_value=1,
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

        mock_tab = AsyncMock()
        mock_tab.get_content = AsyncMock(return_value='csrf = "v1";')
        mock_browser = AsyncMock()
        mock_browser.get = AsyncMock(return_value=mock_tab)
        mock_browser.stop = MagicMock()
        mock_browser.main_tab = mock_tab

        mock_inject = AsyncMock(return_value=1)

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
        mock_browser.main_tab = AsyncMock()

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


class TestExtractTokenExtractionField:
    """Tests for extract_token respecting the extraction field."""

    def test_extraction_http_uses_session_get(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="http",
        )
        mock_resp = MagicMock()
        mock_resp.text = 'csrf = "abc";'
        mock_resp.raise_for_status = MagicMock()

        with patch.object(session, "get", return_value=mock_resp):
            value = extract_token(session, token, "https://example.com")
        assert value == "abc"

    def test_extraction_browser_raises_needs_browser(self) -> None:
        from graftpunk.tokens import BrowserExtractionNeeded

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="browser",
        )
        with pytest.raises(BrowserExtractionNeeded):
            extract_token(session, token, "https://example.com")

    def test_extraction_auto_tries_http_first(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="auto",
        )
        mock_resp = MagicMock()
        mock_resp.text = 'csrf = "abc";'
        mock_resp.raise_for_status = MagicMock()

        with patch.object(session, "get", return_value=mock_resp):
            value = extract_token(session, token, "https://example.com")
        assert value == "abc"

    def test_extraction_auto_raises_needs_browser_on_timeout(self) -> None:
        from graftpunk.tokens import BrowserExtractionNeeded

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="auto",
        )

        with (
            patch.object(session, "get", side_effect=requests.exceptions.ReadTimeout("timeout")),
            pytest.raises(BrowserExtractionNeeded),
        ):
            extract_token(session, token, "https://example.com")

    def test_extraction_auto_raises_needs_browser_on_403(self) -> None:
        from graftpunk.tokens import BrowserExtractionNeeded

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="auto",
        )
        mock_resp = MagicMock()
        mock_resp.status_code = 403
        mock_resp.raise_for_status = MagicMock(
            side_effect=requests.exceptions.HTTPError(response=mock_resp)
        )

        with (
            patch.object(session, "get", return_value=mock_resp),
            pytest.raises(BrowserExtractionNeeded),
        ):
            extract_token(session, token, "https://example.com")

    def test_extraction_http_raises_original_on_timeout(self) -> None:
        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="page",
            pattern=r'csrf = "([^"]+)"',
            page_url="/app",
            extraction="http",
        )

        with (
            patch.object(session, "get", side_effect=requests.exceptions.ReadTimeout("timeout")),
            pytest.raises(requests.exceptions.ReadTimeout),
        ):
            extract_token(session, token, "https://example.com")

    def test_cookie_source_ignores_extraction_field(self) -> None:
        session = requests.Session()
        session.cookies.set("csrf", "val")
        token = Token(
            name="X-CSRF",
            source="cookie",
            cookie_name="csrf",
            extraction="browser",
        )
        value = extract_token(session, token, "https://example.com")
        assert value == "val"

    def test_extraction_auto_response_header_falls_back_on_error(self) -> None:
        from graftpunk.tokens import BrowserExtractionNeeded

        session = requests.Session()
        token = Token(
            name="X-CSRF",
            source="response_header",
            response_header="X-Token",
            page_url="/api",
            extraction="auto",
        )

        with (
            patch.object(
                session,
                "head",
                side_effect=requests.exceptions.ConnectionError("refused"),
            ),
            pytest.raises(BrowserExtractionNeeded),
        ):
            extract_token(session, token, "https://example.com")
